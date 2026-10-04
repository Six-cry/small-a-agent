"""只向隔离测试 Chroma 增加图片检索子块，用于父子检索效果验证。

该脚本不会连接或修改生产 Collection。它复用测试库中已有的完整图片
父块，只为尚未拥有子块的图片生成短文本并调用 Embedding，因此无需重做
原有 655 个 Chunk 的向量。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from ...rag.image_parent_child import (
    IMAGE_CHILD_CONTENT_TYPE,
    expand_image_parent_child_documents,
)
from .build_structured_test_index import (
    STRUCTURED_TEST_COLLECTION_NAME,
    STRUCTURED_TEST_CHROMA_DIR,
    create_structured_test_store,
)


REPORT_DIR = Path(__file__).resolve().parent / "reports" / "image_parent_child"


def add_missing_children() -> dict[str, object]:
    store = create_structured_test_store()
    before_documents = store.get_all_documents()
    before_ids = {
        str(document.metadata["chunk_id"]) for document in before_documents
    }
    expanded = expand_image_parent_child_documents(before_documents)
    missing_children = [
        document
        for document in expanded
        if document.metadata.get("content_type") == IMAGE_CHILD_CONTENT_TYPE
        and str(document.metadata["chunk_id"]) not in before_ids
    ]
    written_ids = store.add_chunks(missing_children)
    after_documents = store.get_all_documents()
    content_types = Counter(
        str(document.metadata.get("content_type", "unknown"))
        for document in after_documents
    )
    summary: dict[str, object] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "chroma_dir": str(STRUCTURED_TEST_CHROMA_DIR.resolve()),
        "collection_name": STRUCTURED_TEST_COLLECTION_NAME,
        "before_count": len(before_documents),
        "new_child_count": len(written_ids),
        "after_count": len(after_documents),
        "content_type_counts": dict(sorted(content_types.items())),
        "production_modified": False,
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "ADD_CHILDREN_REPORT.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# 图片父子 Chunk 隔离测试索引报告",
        "",
        f"- 测试 Collection：`{STRUCTURED_TEST_COLLECTION_NAME}`",
        f"- 写入前：{len(before_documents)} 个 Chunk",
        f"- 本次新增图片检索子块：{len(written_ids)} 个",
        f"- 写入后：{len(after_documents)} 个 Chunk",
        f"- 内容类型：`{dict(sorted(content_types.items()))}`",
        "- 生产 Chroma：未修改",
    ]
    (REPORT_DIR / "ADD_CHILDREN_REPORT.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    try:
        summary = add_missing_children()
    except Exception as exc:
        print(f"[图片子块测试索引失败] {type(exc).__name__}: {exc}")
        return 1
    print(f"隔离测试库写入前：{summary['before_count']}")
    print(f"新增图片检索子块：{summary['new_child_count']}")
    print(f"隔离测试库写入后：{summary['after_count']}")
    print(f"报告：{REPORT_DIR / 'ADD_CHILDREN_REPORT.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
