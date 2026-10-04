"""观察知识库检索距离。

使用方法：

python -m aa_my_agent.rag.retrieval_probe "问题" --k 5
"""

import argparse

from .vector_store import (
    VectorStoreService,
)


def print_result(
    index: int,
    document,
    distance: float,
) -> None:
    """打印一条检索结果。"""
    source = document.metadata.get(
        "source",
        "未知来源",
    )

    page = document.metadata.get("page")

    page_text = (
        f"第 {page + 1} 页"
        if isinstance(page, int)
        else "无页码"
    )

    chunk_index = document.metadata.get(
        "chunk_index",
        "未知",
    )

    content = (
        document.page_content
        .strip()
        .replace("\n", " ")
    )

    print(f"\n结果 {index}")
    print(f"距离：{distance:.6f}")
    print(f"来源：{source}")
    print(f"位置：{page_text}")
    print(
        "Chunk Index："
        f"{chunk_index}"
    )
    print(
        "内容："
        f"{content[:500]}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "查看 RAG 检索结果及原始距离"
        )
    )

    parser.add_argument(
        "query",
        type=str,
        help="需要测试的检索问题",
    )

    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="返回候选结果数量",
    )

    args = parser.parse_args()

    if not args.query.strip():
        raise ValueError(
            "query 不能为空"
        )

    if (
        isinstance(args.k, bool)
        or args.k <= 0
    ):
        raise ValueError(
            "k 必须是正整数"
        )

    vector_store = (
        VectorStoreService()
    )

    results = (
        vector_store.search_with_scores(
            query=args.query.strip(),
            k=args.k,
        )
    )

    print(
        "\n查询："
        f"{args.query.strip()}"
    )

    print(
        "候选结果数量："
        f"{len(results)}"
    )

    print(
        "说明：距离越小越相关，"
        "距离越大越不相关。"
    )

    if not results:
        print(
            "\n知识库中没有任何结果。"
        )
        return

    for index, (
        document,
        distance,
    ) in enumerate(
        results,
        start=1,
    ):
        print_result(
            index=index,
            document=document,
            distance=distance,
        )


if __name__ == "__main__":
    main()