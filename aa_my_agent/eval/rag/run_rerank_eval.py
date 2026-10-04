"""运行本地 Cross-Encoder 精排的隔离对比评测。

本文件不会修改正式 Chroma、Manifest、BM25 或 Agent 配置。
它复用当前混合召回结果，分别计算 RRF 基线和 RRF+Reranker 的 11 道固定题，
用于确认精排是否提高 Top-K 命中率且没有增加无答案误召回。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any


THIS_FILE = Path(__file__).resolve()
REPOSITORY_ROOT = THIS_FILE.parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from aa_my_agent.eval.rag.run_eval import (  # noqa: E402
    DEFAULT_CASES_PATH,
    _to_hit,
    load_cases,
    score_case,
)


def _build_retriever(index_profile: str):
    """按现有正式/隔离配置创建基础混合检索器。"""
    from aa_my_agent.config import (
        RAG_BM25_CANDIDATE_K,
        RAG_BM25_INDEX_PATH,
        RAG_BM25_MIN_MATCHED_TERMS,
        RAG_BM25_MIN_QUERY_COVERAGE,
        RAG_BM25_RESCUE_MAX_RANK,
        RAG_COLLECTION_NAME,
        RAG_DENSE_CANDIDATE_K,
        RAG_MAX_DISTANCE,
        RAG_RETRIEVAL_MODE,
        RAG_RRF_K,
    )
    from aa_my_agent.rag.hybrid_retriever import HybridRetriever
    from aa_my_agent.rag.lexical_index import BM25Index
    from aa_my_agent.rag.vector_store import VectorStoreService

    if index_profile == "structured-test":
        from aa_my_agent.eval.rag.build_structured_test_index import (
            STRUCTURED_TEST_CHROMA_DIR,
            STRUCTURED_TEST_COLLECTION_NAME,
        )

        store = VectorStoreService(
            collection_name=STRUCTURED_TEST_COLLECTION_NAME,
            persist_directory=STRUCTURED_TEST_CHROMA_DIR,
        )
        lexical_path = STRUCTURED_TEST_CHROMA_DIR.parent / "structured_test_bm25.json"
    elif index_profile == "production":
        store = VectorStoreService(
            collection_name=RAG_COLLECTION_NAME,
        )
        lexical_path = RAG_BM25_INDEX_PATH
    else:
        raise ValueError(f"未知索引配置：{index_profile}")

    return HybridRetriever(
        vector_store=store,
        lexical_index=BM25Index(
            index_path=lexical_path,
            collection_name=store.collection_name,
        ),
        mode=RAG_RETRIEVAL_MODE,
        max_distance=RAG_MAX_DISTANCE,
        dense_candidate_k=RAG_DENSE_CANDIDATE_K,
        lexical_candidate_k=RAG_BM25_CANDIDATE_K,
        rrf_k=RAG_RRF_K,
        lexical_rescue_max_rank=RAG_BM25_RESCUE_MAX_RANK,
        lexical_min_query_coverage=RAG_BM25_MIN_QUERY_COVERAGE,
        lexical_min_matched_terms=RAG_BM25_MIN_MATCHED_TERMS,
    )


def _normalized_hits(retrieval, limit: int):
    return [
        _to_hit(
            hit.document,
            hit.dense_distance
            if hit.dense_distance is not None
            else float("inf"),
        )
        for hit in retrieval.hits[:limit]
    ]


def _hit_json(hit) -> dict[str, Any]:
    return {
        "source": hit.source,
        "page": hit.page,
        "distance": hit.distance,
        "content_preview": " ".join(hit.content.split())[:500],
    }


def _write_report(
    report_name: str,
    cases_path: Path,
    index_profile: str,
    model_name: str,
    top_k: int,
    candidate_k: int,
    rows: list[dict[str, Any]],
) -> tuple[Path, Path]:
    reports_dir = THIS_FILE.with_name("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(char for char in report_name if char.isalnum() or char in "-_").strip("-_")
    if not safe_name:
        raise ValueError("report-name 至少要包含一个字母、数字、横线或下划线")

    markdown_path = reports_dir / f"{safe_name}.md"
    json_path = reports_dir / f"{safe_name}.json"
    baseline_passed = sum(bool(row["baseline"]["passed"]) for row in rows)
    reranked_passed = sum(bool(row["reranked"]["passed"]) for row in rows)
    reranker_errors = sum(bool(row["reranked"].get("error")) for row in rows)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    lines = [
        "# RRF 与 RRF + Reranker 隔离对比报告",
        "",
        f"- 生成时间：{generated_at}",
        f"- 题库：`{cases_path}`",
        f"- 索引配置：`{index_profile}`",
        f"- 精排模型：`{model_name}`",
        f"- 候选池：{candidate_k} 条；最终返回：{top_k} 条",
        f"- RRF 基线：{baseline_passed}/{len(rows)} 通过",
        f"- RRF + Reranker：{reranked_passed}/{len(rows)} 通过",
        f"- 精排错误回退次数：{reranker_errors}",
        "",
        "| ID | 类型 | RRF | Reranker | 排名是否变化 |",
        "|---|---|---:|---:|---|",
    ]
    for row in rows:
        baseline = row["baseline"]
        reranked = row["reranked"]
        lines.append(
            f"| {row['id']} | {row['category']} | "
            f"{'通过' if baseline['passed'] else '失败'} | "
            f"{'通过' if reranked['passed'] else '失败'} | "
            f"{'是' if row['order_changed'] else '否'} |"
        )

    lines.extend(["", "## 逐题结果", ""])
    for row in rows:
        lines.extend(
            [
                f"### {row['id']}",
                "",
                f"问题：{row['question']}",
                f"RRF：{'通过' if row['baseline']['passed'] else '失败'}；{row['baseline']['reason']}",
                f"Reranker：{'通过' if row['reranked']['passed'] else '失败'}；{row['reranked']['reason']}",
            ]
        )
        if row["reranked"].get("error"):
            lines.append(f"精排错误：{row['reranked']['error']}")
        lines.extend(["", "RRF 前 K 条：", ""])
        for index, hit in enumerate(row["baseline"]["hits"], start=1):
            lines.append(f"{index}. `{hit['source']}` 第 {hit['page']} 页：{hit['content_preview']}")
        lines.extend(["", "Reranker 前 K 条：", ""])
        for index, hit in enumerate(row["reranked"]["hits"], start=1):
            lines.append(f"{index}. `{hit['source']}` 第 {hit['page']} 页：{hit['content_preview']}")
        lines.append("")

    markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    payload = {
        "generated_at": generated_at,
        "cases_path": str(cases_path),
        "index_profile": index_profile,
        "model_name": model_name,
        "top_k": top_k,
        "candidate_k": candidate_k,
        "summary": {
            "total": len(rows),
            "baseline_passed": baseline_passed,
            "reranked_passed": reranked_passed,
            "reranker_errors": reranker_errors,
        },
        "results": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return markdown_path, json_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 RRF 与本地 Reranker 隔离对比")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--model", default=os.getenv("RAG_RERANKER_MODEL", ""), help="本地或已缓存的 Cross-Encoder 模型路径")
    parser.add_argument("--device", default=os.getenv("RAG_RERANKER_DEVICE", ""))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--report-name", default="after_reranker_isolated")
    parser.add_argument("--index-profile", choices=("production", "structured-test"), default="production")
    parser.add_argument("--only", help="只运行指定题目ID，多个ID用英文逗号分隔")
    parser.add_argument("--sync", action="store_true", help="运行前同步知识库；默认不触发同步")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.top_k <= 0 or args.candidate_k <= 0 or args.candidate_k < args.top_k:
        print("错误：需要满足 candidate-k >= top-k > 0", file=sys.stderr)
        return 2
    if not args.model.strip():
        print("错误：必须通过 --model 或 RAG_RERANKER_MODEL 指定本地 Cross-Encoder 模型", file=sys.stderr)
        return 2

    try:
        cases_path = args.cases.resolve()
        _payload, cases = load_cases(cases_path)
    except ValueError as exc:
        print(f"题库检查失败：{exc}", file=sys.stderr)
        return 2

    if args.only:
        selected_ids = {value.strip() for value in args.only.split(",") if value.strip()}
        known_ids = {str(case.get("id")) for case in cases}
        unknown_ids = sorted(selected_ids - known_ids)
        if unknown_ids:
            print("错误：--only 包含未知题目ID：" + ", ".join(unknown_ids), file=sys.stderr)
            return 2
        cases = [
            {**case, "enabled": bool(case.get("enabled", False)) and str(case.get("id")) in selected_ids}
            for case in cases
        ]

    enabled_cases = [case for case in cases if case.get("enabled", False)]
    print(f"题库格式正确：共 {len(cases)} 题，已开启 {len(enabled_cases)} 题。")
    print("本脚本只读取现有索引；只有显式指定 --sync 才会触发知识库同步。")
    if args.index_profile == "structured-test":
        print("使用隔离结构化测试 Chroma，不会修改正式库。")

    if args.sync:
        from aa_my_agent.rag.auto_sync import ensure_rag_index_current

        sync = ensure_rag_index_current(output=print, force=True)
        if not sync.succeeded:
            print(f"知识库同步失败：{sync.message}", file=sys.stderr)
            return 2

    try:
        from aa_my_agent.rag.reranked_retriever import RerankedRetriever
        from aa_my_agent.rag.reranker import CrossEncoderReranker

        base_retriever = _build_retriever(args.index_profile)
        reranked_retriever = RerankedRetriever(
            base_retriever,
            CrossEncoderReranker(
                args.model,
                device=args.device or None,
            ),
            candidate_k=args.candidate_k,
        )
    except Exception as exc:
        print(f"精排器初始化失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    rows: list[dict[str, Any]] = []
    for case in enabled_cases:
        started = perf_counter()
        try:
            base_result = base_retriever.retrieve(case["question"], final_k=args.candidate_k)
            baseline_hits = _normalized_hits(base_result, args.top_k)
            baseline_passed, baseline_reason = score_case(case, baseline_hits)
            reranked_result = reranked_retriever.rerank_result(
                case["question"],
                base_result,
                final_k=args.top_k,
            )
            reranked_hits = _normalized_hits(reranked_result, args.top_k)
            reranked_passed, reranked_reason = score_case(case, reranked_hits)
            baseline_ids = [hit.document.metadata.get("chunk_id") for hit in base_result.hits[:args.top_k]]
            reranked_ids = [hit.document.metadata.get("chunk_id") for hit in reranked_result.hits]
            row = {
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
                "latency_seconds": perf_counter() - started,
                "order_changed": baseline_ids != reranked_ids,
                "baseline": {
                    "passed": baseline_passed,
                    "reason": baseline_reason,
                    "hits": [_hit_json(hit) for hit in baseline_hits],
                },
                "reranked": {
                    "passed": reranked_passed,
                    "reason": reranked_reason,
                    "error": reranked_result.reranker_error,
                    "hits": [_hit_json(hit) for hit in reranked_hits],
                },
            }
        except Exception as exc:
            row = {
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
                "latency_seconds": perf_counter() - started,
                "order_changed": False,
                "baseline": {"passed": False, "reason": f"执行失败：{type(exc).__name__}: {exc}", "hits": []},
                "reranked": {"passed": False, "reason": "未执行", "error": None, "hits": []},
            }
        rows.append(row)
        print(
            f"[{ '通过' if row['baseline']['passed'] else '失败' }] {row['id']} RRF：{row['baseline']['reason']}；"
            f"Reranker：{row['reranked']['reason']}"
        )

    markdown_path, json_path = _write_report(
        report_name=args.report_name,
        cases_path=cases_path,
        index_profile=args.index_profile,
        model_name=args.model,
        top_k=args.top_k,
        candidate_k=args.candidate_k,
        rows=rows,
    )
    print(f"报告已生成：{markdown_path}")
    print(f"原始数据已生成：{json_path}")
    return 0 if all(row["baseline"]["passed"] and row["reranked"]["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
