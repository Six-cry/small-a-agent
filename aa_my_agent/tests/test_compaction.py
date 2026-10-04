import importlib.util
import sys, json
import tempfile
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = PACKAGE_DIR / "context" / "compaction.py"


def load_compaction_module():
    """使用假的 config 加载模块，测试期间不会访问真实模型或密钥。"""
    fake_config = types.ModuleType("aa_my_agent.config")
    fake_config.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=None))
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
    fake_config.TOOL_RESULTS_DIR = Path(tempfile.gettempdir()) / "aa-agent-test-results"
    fake_config.TRANSCRIPT_DIR = Path(tempfile.gettempdir()) / "aa-agent-test-transcripts"

    package = sys.modules.setdefault("aa_my_agent", types.ModuleType("aa_my_agent"))
    package.__path__ = [str(PACKAGE_DIR)]
    previous_context = sys.modules.get("aa_my_agent.context")
    context_package = previous_context or types.ModuleType("aa_my_agent.context")
    context_package.__path__ = [str(PACKAGE_DIR / "context")]
    sys.modules["aa_my_agent.context"] = context_package

    previous_config = sys.modules.get("aa_my_agent.config")
    sys.modules["aa_my_agent.config"] = fake_config
    name = "aa_my_agent.context.compaction_under_test"
    try:
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_config is None:
            sys.modules.pop("aa_my_agent.config", None)
        else:
            sys.modules["aa_my_agent.config"] = previous_config
        if previous_context is None:
            sys.modules.pop("aa_my_agent.context", None)
        else:
            sys.modules["aa_my_agent.context"] = previous_context


def text_messages(count: int) -> list[dict]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message-{index}",
        }
        for index in range(count)
    ]


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.module = load_compaction_module()
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.module.TOOL_RESULTS_DIR = root / "tool-results"
        self.module.TRANSCRIPT_DIR = root / "transcripts"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_single_large_result_is_persisted_even_under_total_budget(self):
        output = "旅" * 30_001
        messages = [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tool/unsafe",
                "content": output,
            }],
        }]

        self.module.tool_result_budget(messages, max_bytes=200_000)

        marker = messages[0]["content"][0]["content"]
        self.assertTrue(marker.startswith("<persisted-output>"))
        saved_files = list(self.module.TOOL_RESULTS_DIR.glob("*.txt"))
        self.assertEqual(len(saved_files), 1)
        self.assertEqual(saved_files[0].read_text(encoding="utf-8"), output)
        self.assertNotIn("/", saved_files[0].name)

    def test_total_budget_can_force_persist_medium_results(self):
        messages = [{
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"tool-{index}",
                    "content": "x" * 500,
                }
                for index in range(3)
            ],
        }]

        self.module.tool_result_budget(messages, max_bytes=1_100)

        contents = [block["content"] for block in messages[0]["content"]]
        self.assertTrue(any(text.startswith("<persisted-output>") for text in contents))

    def test_snip_reserves_one_slot_for_placeholder(self):
        compacted = self.module.snip_compact(text_messages(60), max_messages=50)

        self.assertEqual(len(compacted), 50)
        self.assertTrue(
            any(
                message.get("content", "").startswith(self.module.SNIPPED_MARKER)
                for message in compacted
            )
        )

    def test_pipeline_does_not_snip_small_history_by_message_count(self):
        messages = text_messages(60)
        fact = "返程必须在10月7日20点前到家"
        messages[8] = {"role": "user", "content": fact}

        did_summarize = self.module.apply_compaction_pipeline(messages)

        self.assertFalse(did_summarize)
        self.assertEqual(len(messages), 60)
        self.assertIn(fact, json.dumps(messages, ensure_ascii=False))

    def test_pipeline_does_not_micro_compact_arbitrary_tool_facts(self):
        messages = [{"role": "user", "content": "规划行程"}]
        fact = "CRITICAL_BOOKING_ID=BOOK-8137"
        for index in range(4):
            result = "x" * 10_000
            if index == 0:
                result = "x" * 500 + fact + "x" * 9_500
            messages.extend([
                {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": f"tool-{index}",
                        "name": "web_search",
                    }],
                },
                {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": f"tool-{index}",
                        "content": result,
                    }],
                },
            ])

        did_summarize = self.module.apply_compaction_pipeline(messages)

        self.assertFalse(did_summarize)
        self.assertIn(fact, json.dumps(messages, ensure_ascii=False))
        self.assertNotIn(self.module.TOOL_RESULT_MARKER, json.dumps(messages))

    def test_summary_input_keeps_middle_of_large_history(self):
        fact = "CRITICAL-MIDDLE-FACT:预算8137元"
        messages = [
            {"role": "user", "content": "a" * 40_000},
            {"role": "user", "content": fact},
            {"role": "user", "content": "b" * 50_000},
        ]

        payload = self.module._summary_input(messages, [])

        self.assertIn(fact, payload)
        self.assertGreater(len(payload), 80_000)

    def test_summary_call_error_uses_fallback(self):
        fact = "用户确认预算8137元"
        messages = [{"role": "user", "content": fact}]
        self.module.client.messages.create = (
            lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("timeout"))
        )

        summary = self.module._summarize_history(messages, [])

        self.assertIn(fact, summary)

    def test_fallback_keeps_recent_mcp_fact(self):
        fact = "AMAP-DISTANCE=2603"
        messages = [
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "mcp-1",
                    "name": "mcp__amap__maps_distance",
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "mcp-1",
                    "content": fact,
                }],
            },
        ]

        summary = self.module._fallback_summary(messages, [])

        self.assertIn(fact, summary)

    def test_fallback_stays_below_default_target(self):
        messages = [
            {
                "role": "user",
                "content": "[Compacted conversation summary]\n" + "s" * 12_000,
            }
        ]
        messages.extend(text_messages(30))

        summary = self.module._fallback_summary(messages, [{"content": "t" * 8_000}])

        candidate = [{
            "role": "user",
            "content": "[Compacted conversation summary]\n" + summary,
        }]
        self.assertLessEqual(self.module.estimate_size(candidate), 30_000)

    def test_compact_history_keeps_recent_messages_exactly(self):
        messages = text_messages(14)
        self.module._write_transcript = lambda _messages: Path("transcript.jsonl")
        captured = {}

        def fake_summary(old_messages, todos=None):
            captured["old"] = list(old_messages)
            captured["todos"] = todos
            return "旅行摘要" * 50

        self.module._summarize_history = fake_summary
        compacted = self.module.compact_history(
            messages,
            todos=[{"content": "确认日期", "status": "waiting_for_user"}],
            keep_recent=4,
        )

        # 为了让摘要 user 消息后面从 assistant 开始，会额外保留第 9 条。
        self.assertEqual(compacted[1:], messages[9:])
        # 摘要覆盖完整历史，近期原文只是额外的高保真副本。
        self.assertEqual(captured["old"], messages)
        self.assertEqual(captured["todos"][0]["status"], "waiting_for_user")

    def test_pipeline_summarizes_before_lossy_snip(self):
        messages = text_messages(60)
        self.module.CONTEXT_LIMIT = 1
        self.module._write_transcript = lambda _messages: Path("transcript.jsonl")
        captured = {}

        def fake_summary(old_messages, todos=None):
            captured["count"] = len(old_messages)
            return "summary" * 40

        self.module._summarize_history = fake_summary
        did_summarize = self.module.apply_compaction_pipeline(messages)

        self.assertTrue(did_summarize)
        # 摘要器看到完整历史，而不是 snip 后只剩下的 50 条。
        self.assertEqual(captured["count"], 60)
        self.assertLessEqual(self.module.estimate_size(messages), 30_000)

    def test_l4_compaction_reaches_target_size(self):
        messages = []
        for index in range(6):
            messages.extend([
                {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": f"tool-{index}",
                        "name": "web_search",
                    }],
                },
                {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": f"tool-{index}",
                        "content": "搜索结果" * 1_000,
                    }],
                },
            ])

        self.module.CONTEXT_LIMIT = 1
        self.module.COMPACT_TARGET = 1_000
        self.module._write_transcript = lambda _messages: Path("transcript.jsonl")
        self.module._summarize_history = (
            lambda _messages, _todos=None: "核心摘要" * 50
        )

        did_summarize = self.module.apply_compaction_pipeline(messages)

        self.assertTrue(did_summarize)
        self.assertLessEqual(self.module.estimate_size(messages), 1_000)

    def test_recent_tail_does_not_split_tool_pair(self):
        messages = text_messages(8)
        messages.extend([
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tool-1", "name": "web_search"}],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "result",
                }],
            },
        ])
        self.module._write_transcript = lambda _messages: Path("transcript.jsonl")
        self.module._summarize_history = (
            lambda _messages, _todos=None: "summary" * 40
        )

        compacted = self.module.compact_history(messages, keep_recent=1)

        self.assertEqual(compacted[1:], messages[-2:])

    def test_transcript_preserves_chinese_and_uses_unique_names(self):
        messages = [{"role": "user", "content": "北京三日游"}]
        first = self.module._write_transcript(messages)
        second = self.module._write_transcript(messages)

        self.assertNotEqual(first, second)
        self.assertIn("北京三日游", first.read_text(encoding="utf-8"))

    def test_bad_model_summary_uses_deterministic_fallback(self):
        response = types.SimpleNamespace(
            stop_reason="max_tokens",
            content=[types.SimpleNamespace(type="thinking", thinking="hidden")],
            usage=types.SimpleNamespace(
                input_tokens=100,
                output_tokens=2_400,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )
        self.module.client.messages.create = lambda **_kwargs: response
        self.module.record_model_response = lambda *_args: {
            "input": 100,
            "output": 2_400,
            "cache_read": 0,
            "cache_create": 0,
        }
        messages = [
            {
                "role": "user",
                "content": "预算8137元，返程必须在10月7日20:00前到家。",
            },
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "time-1",
                    "name": "get_current_time",
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "time-1",
                    "content": "当前时间：2026-09-05 17:30:00 +08:00",
                }],
            },
        ]

        summary = self.module._summarize_history(messages, [])

        self.assertIn("8137", summary)
        self.assertIn("10月7日20:00", summary)
        self.assertIn("2026-09-05 17:30:00", summary)
        self.assertNotIn("摘要为空", summary)

    def test_micro_compaction_does_not_expose_transcript_path(self):
        messages = [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": f"tool-{index}",
                "content": "结果" * 1_000,
            } for index in range(4)],
        }]

        compacted = self.module.micro_compact(
            messages,
            transcript_path=Path("secret-transcript.jsonl"),
            keep_recent=1,
        )

        old_contents = [
            block["content"] for block in compacted[0]["content"][:-1]
        ]
        self.assertTrue(all("secret-transcript" not in text for text in old_contents))
        self.assertTrue(all("Do not inspect" in text for text in old_contents))
        self.assertTrue(all("Preview:" in text for text in old_contents))

    def test_fallback_inherits_previous_compaction_summary(self):
        """连续L4必须继承上一轮摘要中的重要事实。"""
        fact = "BASELINE-FACT:预算8137元"

        messages = [{
            "role": "user",
            "content": (
                "[Compacted conversation summary]\n"
                "## 已确认事实\n"
                f"- {fact}"
            ),
        }]

        summary = self.module._fallback_summary(
            messages,
            [],
        )

        self.assertIn(fact, summary)

    def test_three_fallbacks_preserve_initial_request(self):
        """连续三次兜底摘要后，最初需求仍然存在。"""
        fact = (
            "INITIAL-REQUEST:"
            "天津出发，3位成人，2026年10月1日至10月5日，"
            "要求舒适奢华"
        )

        messages = [{
            "role": "user",
            "content": fact,
        }]

        for index in range(3):
            summary = self.module._fallback_summary(
                messages,
                [],
            )

            messages = [
                {
                    "role": "user",
                    "content": (
                        "[Compacted conversation summary]\n"
                        + summary
                    ),
                },
                {
                    "role": "user",
                    "content": f"第{index + 1}次压缩后的新消息",
                },
            ]

        serialized = json.dumps(
            messages,
            ensure_ascii=False,
        )

        self.assertIn(fact, serialized)


if __name__ == "__main__":
    unittest.main()
