"""L2 压缩“重要纸条是否被扔”检查。

背景
----
真实事故：模型先调用 get_current_time 拿到了当前日期（2026-09-03），
随后又连续执行了多个工具调用。等它最后写旅游计划文档时，日期已经从
上下文中消失——被 L2 micro_compact 换成了占位符，模型只能瞎猜年份。

这个检查模拟同一过程，验证“日期纸条”经过几轮压缩后是否还在。

用法（在项目根目录执行，不调真实模型、不改任何生产代码）：
    python tests/test_l2_fact_preservation.py

输出说明
--------
场景 A：先放“日期纸条”，再连续执行 6 个工具调用  → 期望：纸条还在
场景 B：先放“日期纸条”，再连续执行 2 个工具调用  → 期望：纸条还在
（B 是对照：2 个后续结果不会触发压缩，用来证明问题出在“超过 3 个就扔”）
"""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

PACKAGE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = PACKAGE_DIR / "context" / "compaction.py"


class L2FactPreservationBaselineTests(unittest.TestCase):
    def test_old_time_fact_survives_six_later_tool_results(self):
        """小上下文不会仅因后续工具结果数量增加而丢失日期事实。"""
        module = load_compaction_module()
        module.emit = lambda *args, **kwargs: None

        _fact_token, outcome = run_scenario(module, followup_count=6)

        self.assertTrue(outcome["alive"])


def load_compaction_module():
    """用假的 config 加载 compaction.py，避免访问真实模型/密钥/落盘目录。"""
    fake_config = types.ModuleType("aa_my_agent.config")
    fake_config.client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=None)
    )
    fake_config.MODEL = "test-model"
    fake_config.CONTEXT_LIMIT = 50_000
    fake_config.COMPACT_TARGET = 30_000
    fake_config.L2_TRIGGER_CHARS = 35_000
    fake_config.L2_TARGET_CHARS = 30_000
    fake_config.L2_MIN_RECLAIM_CHARS = 2_000
    fake_config.L2_RESULT_PREVIEW_CHARS = 400
    fake_config.KEEP_RECENT_TOOL_RESULTS = 3
    fake_config.PERSIST_THRESHOLD = 30_000
    fake_config.TOOL_RESULT_BUDGET_BYTES = 200_000
    fake_config.TOOL_RESULTS_DIR = Path(tempfile.gettempdir()) / "l2-check-results"
    fake_config.TRANSCRIPT_DIR = Path(tempfile.gettempdir()) / "l2-check-transcripts"

    package = sys.modules.setdefault(
        "aa_my_agent", types.ModuleType("aa_my_agent")
    )
    package.__path__ = [str(PACKAGE_DIR)]

    context_package = types.ModuleType("aa_my_agent.context")
    context_package.__path__ = [str(PACKAGE_DIR / "context")]
    sys.modules["aa_my_agent.context"] = context_package

    previous_config = sys.modules.get("aa_my_agent.config")
    sys.modules["aa_my_agent.config"] = fake_config
    try:
        spec = importlib.util.spec_from_file_location(
            "aa_my_agent.context.compaction_under_check", MODULE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_config is None:
            sys.modules.pop("aa_my_agent.config", None)
        else:
            sys.modules["aa_my_agent.config"] = previous_config


def _tool_pair(tool_id: str, tool_name: str, result: str) -> list[dict]:
    """构造一轮完整的工具调用：assistant(tool_use) + user(tool_result)。"""
    return [
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_id,
                "name": tool_name,
                "input": {},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result,
            }],
        },
    ]


def run_scenario(module, followup_count: int) -> tuple[str, dict]:
    """
    模拟一轮真实会话：
    1. 用户提问；
    2. 先放一张“日期纸条”（get_current_time 结果，约 200 字符）；
    3. 再连续执行 followup_count 个 web_search 工具调用；
    4. 每执行完一轮就调用一次真实的压缩管线（与 agent.py 每轮一致）。
    最后返回：唯一标记是否还留在上下文里。
    """
    with tempfile.TemporaryDirectory(prefix="l2-check-") as tmp:
        module.TRANSCRIPT_DIR = Path(tmp) / "transcripts"
        module.TOOL_RESULTS_DIR = Path(tmp) / "tool-results"

        messages = [
            {"role": "user", "content": "请为我做一个国庆节从南京去哈尔滨的旅游计划"}
        ]

        # “日期纸条”：内容和真实 get_current_time 输出一样长（>120 字符才会被压）
        today = datetime.now().strftime("%Y-%m-%d")
        fact_token = f"FACT-DATE:{today}"
        time_result = fact_token + " 当前日期测试占位填充" + "x" * 150

        messages += _tool_pair("time-1", "get_current_time", time_result)

        for index in range(1, followup_count + 1):
            messages += _tool_pair(
                f"tool-{index}",
                "web_search",
                "哈尔滨国庆旅游搜索结果" * 50,  # >120 字符
            )
            module.apply_compaction_pipeline(messages)

        serialized = json.dumps(messages, ensure_ascii=False)
        return fact_token, {
            "alive": fact_token in serialized,
            "markers": serialized.count(module.TOOL_RESULT_MARKER),
            "messages": len(messages),
        }


def main() -> int:
    module = load_compaction_module()
    # 静音压缩管线的内部日志，只看我们的检查结论。
    module.emit = lambda *args, **kwargs: None

    print("=" * 60)
    print("检查：L2 压缩会不会把“日期纸条”扔掉？")
    print("=" * 60)

    results = {}
    for name, count in (("A：拿到日期后又干了 6 件事", 6),
                        ("B：拿到日期后又干了 2 件事", 2)):
        fact_token, outcome = run_scenario(module, count)
        results[name] = outcome
        verdict = "纸条还在 ✓" if outcome["alive"] else "纸条被扔了 ✗"
        print(f"\n场景 {name}")
        print(f"  上下文消息数      : {outcome['messages']}")
        print(f"  被替换的占位符数  : {outcome['markers']}")
        print(f"  结论              : {verdict}")

    print("\n" + "=" * 60)
    a_alive = results["A：拿到日期后又干了 6 件事"]["alive"]
    if a_alive:
        print("结论：检查通过 —— 日期能扛过后续 6 个工具调用，问题已修复。")
        return 0

    print("结论：检查失败 —— 日期纸条被 L2 压缩扔掉了（当前代码确实存在该问题）。")
    print("提示：先放纸条、再干 2 件事时没事，说明问题出在“工具结果超过 3 个")
    print("就无条件压缩最旧结果”这条规则上，与真实事故的日志完全吻合。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
