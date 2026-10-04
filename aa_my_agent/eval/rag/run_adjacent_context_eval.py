"""用正式 Chroma 中的真实样本验证邻接 Chunk 与引用元数据。

本脚本不执行向量查询、不调用 Embedding 或聊天模型，也不修改 Chroma。
它定位已经人工核对过的第 4 页表 1，检查核心表格前后的原文 Chunk
能否按文件级 chunk_index 被完整补回，并生成可长期保留的评测报告。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from aa_my_agent.rag.adjacent_context import AdjacentChunkExpander
from aa_my_agent.rag.hybrid_retriever import HybridSearchHit
from aa_my_agent.rag.image_parent_child import IMAGE_CHILD_CONTENT_TYPE
from aa_my_agent.rag.vector_store import VectorStoreService


REPORTS_DIR = Path(__file__).resolve().parent / "reports"
SOURCE = "考虑排队时延的系统保护通信网络路由选择算法_刘川.pdf"
HUMAN_PAGE = 4


def _preview(text: str) -> str:
    return " ".join(text.split())[:500]


def run() -> dict:
    store = VectorStoreService()
    documents = store.get_all_documents()
    core_candidates = [
        document
        for document in documents
        if document.metadata.get("source") == SOURCE
        and document.metadata.get("page") == HUMAN_PAGE - 1
        and document.metadata.get("content_type") == "table"
        and "2→3→4→7→11" in document.page_content
        and "2→5→6→9→10→11" in document.page_content
    ]
    if len(core_candidates) != 1:
        raise RuntimeError(f"代表性核心表格应唯一，实际找到 {len(core_candidates)} 个")

    core = core_candidates[0]
    hit = HybridSearchHit(
        document=core,
        rrf_score=0.03,
        accepted=True,
        acceptance_reason="verified-fixture",
    )
    expanded = AdjacentChunkExpander(
        store,
        window_size=1,
        max_total_chars=8000,
    ).expand((hit,))
    adjacent = expanded[0].adjacent_chunks
    by_offset = {item.offset: item.document for item in adjacent}
    previous = by_offset.get(-1)
    following = by_offset.get(1)

    checks = {
        "found_previous": previous is not None,
        "found_following": following is not None,
        "previous_is_immediate": (
            previous is not None
            and previous.metadata.get("chunk_index")
            == core.metadata.get("chunk_index") - 1
        ),
        "following_is_immediate": (
            following is not None
            and following.metadata.get("chunk_index")
            == core.metadata.get("chunk_index") + 1
        ),
        "previous_has_network_setup": (
            previous is not None
            and "处理时延" in previous.page_content
            and "0.1ms" in previous.page_content.replace(" ", "")
        ),
        "following_has_figure_3_context": (
            following is not None
            and "图 3" in following.page_content
            and "时延对比" in following.page_content
        ),
        "all_have_traceable_metadata": all(
            isinstance(document.metadata.get("chunk_id"), str)
            and bool(document.metadata.get("chunk_id"))
            and document.metadata.get("source") == SOURCE
            and document.metadata.get("page") == HUMAN_PAGE - 1
            for document in [core, *[item.document for item in adjacent]]
        ),
        "no_image_retrieval_child": all(
            item.document.metadata.get("content_type") != IMAGE_CHILD_CONTENT_TYPE
            for item in adjacent
        ),
    }
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": SOURCE,
        "page": HUMAN_PAGE,
        "passed": all(checks.values()),
        "checks": checks,
        "core": {
            "chunk_id": core.metadata.get("chunk_id"),
            "chunk_index": core.metadata.get("chunk_index"),
            "content_type": core.metadata.get("content_type"),
            "preview": _preview(core.page_content),
        },
        "adjacent": [
            {
                "offset": item.offset,
                "chunk_id": item.document.metadata.get("chunk_id"),
                "chunk_index": item.document.metadata.get("chunk_index"),
                "content_type": item.document.metadata.get("content_type"),
                "preview": _preview(item.document.page_content),
            }
            for item in adjacent
        ],
        "visual_verification": (
            "已对照原 PDF 第 4 页：图 2 下方网络参数说明、表 1、"
            "图 3 及其说明在版面阅读顺序上连续。"
        ),
    }


def write_report(result: dict) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    markdown_path = REPORTS_DIR / "after_adjacent_context.md"
    json_path = REPORTS_DIR / "after_adjacent_context.json"
    lines = [
        "# 邻接 Chunk 与可靠引用评测",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 代表性来源：`{result['source']}` 第 {result['page']} 页",
        f"- 总结果：{'通过' if result['passed'] else '失败'}",
        f"- 原页核对：{result['visual_verification']}",
        "",
        "## 自动检查",
        "",
    ]
    for name, passed in result["checks"].items():
        lines.append(f"- {'通过' if passed else '失败'}：`{name}`")
    lines.extend(
        [
            "",
            "## 核心证据",
            "",
            f"- Chunk Index：{result['core']['chunk_index']}",
            f"- Chunk ID：`{result['core']['chunk_id']}`",
            f"- 类型：`{result['core']['content_type']}`",
            f"- 摘要：{result['core']['preview']}",
            "",
            "## 相邻上下文",
            "",
        ]
    )
    for item in result["adjacent"]:
        lines.extend(
            [
                f"### Offset {item['offset']:+d}",
                "",
                f"- Chunk Index：{item['chunk_index']}",
                f"- Chunk ID：`{item['chunk_id']}`",
                f"- 类型：`{item['content_type']}`",
                f"- 摘要：{item['preview']}",
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return markdown_path, json_path


def main() -> int:
    result = run()
    markdown_path, json_path = write_report(result)
    print(f"[{'通过' if result['passed'] else '失败'}] 邻接 Chunk 真实样本评测")
    print(f"报告已生成：{markdown_path}")
    print(f"原始数据已生成：{json_path}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
