"""独立管理知识库索引的命令行入口。

日常先运行 status 查看变化，再运行 sync 增量入库；小 A 的 main.py
不会调用本文件。rebuild 仅用于索引损坏后的完整重建，不是日常操作。
"""

import argparse

from .index_status import check_rag_index_status


def main() -> None:
    """提供快速状态、增量同步、深度校验和完整重建。"""
    parser = argparse.ArgumentParser(
        description="独立管理小 A 的 RAG 知识库索引"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=("status", "sync", "verify", "rebuild"),
        help=(
            "status：快速只读检查；sync：增量同步；"
            "verify：完整 Hash 与 Chroma 校验；rebuild：完整重建"
        ),
    )
    args = parser.parse_args()

    if args.command == "status":
        outcome = check_rag_index_status(
            force=True,
            check_interval_seconds=0,
        )
        print("\n知识库快速状态：")
        print(outcome.message)
        for label, paths in (
            ("新增", outcome.added_paths),
            ("修改", outcome.changed_paths),
            ("删除", outcome.deleted_paths),
        ):
            if paths:
                print(f"\n{label}文件：")
                for path in paths:
                    print(f"  - {path}")
        return

    # Chroma、Embedding、Loader 等重量级依赖只在真正需要时导入。
    # 快速 status 不应为这些组件支付启动成本。
    from .index_diff import print_diff_details
    from .index_manager import (
        IndexStateError,
        create_default_index_manager,
    )

    manager = create_default_index_manager()
    if args.command == "verify":
        manifest, diff = manager.status()
        print("\n完整文件变化状态：")
        print_diff_details(diff)
        print(f"\nManifest 已记录文件：{len(manifest.sources)}")
        print(f"Chroma 当前 Chunk：{manager.vector_store.count()}")
        try:
            manager.validate_index_state(manifest)
        except IndexStateError as exc:
            print("\n索引状态：不一致")
            print(exc)
        else:
            print("\n索引状态：一致")
        return

    result = manager.sync() if args.command == "sync" else manager.rebuild()
    print("\n索引操作完成：")
    print(result.summary())


if __name__ == "__main__":
    main()
