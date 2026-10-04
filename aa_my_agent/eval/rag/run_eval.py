"""Run the small, fixed RAG retrieval baseline.

This evaluates retrieval only. It does not call the Agent's chat model, so a
failure points to document parsing, chunking, indexing, or retrieval rather
than answer generation.

Run from the repository root:

    python -m aa_my_agent.eval.rag.run_eval --validate-only
    python -m aa_my_agent.eval.rag.run_eval
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any


THIS_FILE = Path(__file__).resolve()
DEFAULT_CASES_PATH = THIS_FILE.with_name("cases.json")
DEFAULT_REPORTS_DIR = THIS_FILE.with_name("reports")

# Also support: python aa_my_agent/eval/rag/run_eval.py
REPOSITORY_ROOT = THIS_FILE.parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


@dataclass(frozen=True, slots=True)
class Hit:
    """One normalized retrieval result."""

    source: str
    page: int | None
    distance: float
    content: str


@dataclass(frozen=True, slots=True)
class CaseResult:
    """The score and evidence for one enabled test case."""

    case_id: str
    category: str
    question: str
    passed: bool
    reason: str
    latency_seconds: float
    hits: tuple[Hit, ...]


def _non_empty_string(value: Any, field: str, case_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{case_id}: {field} 必须是非空字符串")
    return value.strip()


def _reject_placeholder(value: str, field: str, case_id: str) -> None:
    if "请填写" in value:
        raise ValueError(f"{case_id}: {field} 仍是模板文字，请先填写真实内容")


def load_cases(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load and validate the test-set JSON file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"找不到题库文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"题库不是有效 JSON：{exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("题库最外层必须是 JSON 对象")

    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("题库必须包含 cases 数组")

    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []

    for index, raw_case in enumerate(cases, start=1):
        if not isinstance(raw_case, dict):
            raise ValueError(f"第 {index} 个测试用例必须是 JSON 对象")

        case_id = _non_empty_string(raw_case.get("id"), "id", f"第 {index} 题")
        if case_id in seen_ids:
            raise ValueError(f"测试用例 id 重复：{case_id}")
        seen_ids.add(case_id)

        enabled = raw_case.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"{case_id}: enabled 必须是 true 或 false")

        # Disabled rows are intentionally allowed to contain placeholders.
        if not enabled:
            normalized.append(raw_case)
            continue

        question = _non_empty_string(raw_case.get("question"), "question", case_id)
        category = _non_empty_string(raw_case.get("category"), "category", case_id)
        _reject_placeholder(question, "question", case_id)
        expected = raw_case.get("expected")
        if not isinstance(expected, dict):
            raise ValueError(f"{case_id}: expected 必须是 JSON 对象")

        should_find = expected.get("should_find")
        if not isinstance(should_find, bool):
            raise ValueError(f"{case_id}: expected.should_find 必须是 true 或 false")

        source = expected.get("source")
        pages = expected.get("pages", [])
        keywords = expected.get("keywords", [])
        keyword_mode = expected.get("keyword_mode", "any")

        if not isinstance(pages, list) or any(
            isinstance(page, bool) or not isinstance(page, int) or page <= 0
            for page in pages
        ):
            raise ValueError(f"{case_id}: expected.pages 只能填写正整数页码")

        if not isinstance(keywords, list) or any(
            not isinstance(keyword, str) or not keyword.strip()
            for keyword in keywords
        ):
            raise ValueError(f"{case_id}: expected.keywords 必须是非空字符串数组")

        if keyword_mode not in {"any", "all"}:
            raise ValueError(
                f"{case_id}: expected.keyword_mode 只能是 any 或 all"
            )

        if should_find:
            source = _non_empty_string(source, "expected.source", case_id)
            _reject_placeholder(source, "expected.source", case_id)
            for keyword in keywords:
                _reject_placeholder(keyword, "expected.keywords", case_id)
        elif source not in (None, "") or pages or keywords:
            raise ValueError(
                f"{case_id}: 无答案题的 source 应为 null，pages/keywords 应为空数组"
            )

        normalized.append(
            {
                **raw_case,
                "id": case_id,
                "question": question,
                "category": category,
                "expected": {
                    "should_find": should_find,
                    "source": source.strip() if isinstance(source, str) else None,
                    "pages": pages,
                    "keywords": [keyword.strip() for keyword in keywords],
                    "keyword_mode": keyword_mode,
                },
            }
        )

    return payload, normalized


def _normalize_source(value: str) -> str:
    return value.replace("\\", "/").strip().casefold()


def _normalize_keyword_text(value: str) -> str:
    """Ignore whitespace inserted by PDF text extraction."""
    return "".join(value.casefold().split())


def _source_matches(actual: str, expected: str) -> bool:
    actual_value = _normalize_source(actual)
    expected_value = _normalize_source(expected)
    return actual_value == expected_value or actual_value.endswith(f"/{expected_value}")


def score_case(case: dict[str, Any], hits: list[Hit]) -> tuple[bool, str]:
    """Score one case against results accepted by the production evidence gate."""
    expected = case["expected"]

    if not expected["should_find"]:
        if not hits:
            return True, "没有结果通过当前混合证据门槛"
        return False, f"本应无答案，但有 {len(hits)} 条结果通过混合证据门槛"

    expected_source = expected["source"]
    expected_pages = set(expected["pages"])
    expected_keywords = [
        _normalize_keyword_text(keyword)
        for keyword in expected["keywords"]
    ]
    keyword_mode = expected.get("keyword_mode", "any")

    source_hits = [hit for hit in hits if _source_matches(hit.source, expected_source)]
    if not source_hits:
        return False, "前 K 条结果中没有命中预期文件"

    location_hits = [
        hit
        for hit in source_hits
        if not expected_pages or hit.page in expected_pages
    ]
    if not location_hits:
        return False, "命中了预期文件，但没有命中预期页码"

    if expected_keywords:
        def content_matches(hit: Hit) -> bool:
            normalized_content = _normalize_keyword_text(hit.content)
            matches = [
                keyword in normalized_content
                for keyword in expected_keywords
            ]
            return all(matches) if keyword_mode == "all" else any(matches)

        keyword_hit = any(content_matches(hit) for hit in location_hits)
        if not keyword_hit:
            requirement = "全部" if keyword_mode == "all" else "任一"
            return False, f"文件和页码正确，但内容未包含{requirement}预期关键词"

    return True, "命中了预期文件、页码和内容"


def _to_hit(document: Any, distance: float) -> Hit:
    raw_page = document.metadata.get("page")
    human_page = (
        raw_page + 1
        if isinstance(raw_page, int) and not isinstance(raw_page, bool)
        else None
    )
    return Hit(
        source=str(document.metadata.get("source", "未知来源")),
        page=human_page,
        distance=float(distance),
        content=str(document.page_content),
    )


def run_cases(
    cases: list[dict[str, Any]],
    top_k: int,
    skip_sync: bool,
    index_profile: str = "production",
) -> tuple[list[CaseResult], float]:
    """Query the current vector index and score every enabled case."""
    from aa_my_agent.config import (
        RAG_BM25_CANDIDATE_K,
        RAG_BM25_MIN_MATCHED_TERMS,
        RAG_BM25_MIN_QUERY_COVERAGE,
        RAG_BM25_RESCUE_MAX_RANK,
        RAG_DENSE_CANDIDATE_K,
        RAG_MAX_DISTANCE,
        RAG_RETRIEVAL_MODE,
        RAG_RRF_K,
    )
    from aa_my_agent.rag.auto_sync import ensure_rag_index_current
    from aa_my_agent.rag.hybrid_retriever import HybridRetriever
    from aa_my_agent.rag.lexical_index import BM25Index
    from aa_my_agent.rag.vector_store import VectorStoreService

    if index_profile == "structured-test":
        from aa_my_agent.eval.rag.build_structured_test_index import (
            STRUCTURED_TEST_CHROMA_DIR,
            STRUCTURED_TEST_COLLECTION_NAME,
        )

        # 隔离测试库没有生产 Manifest，绝不能触发正式自动同步。
        skip_sync = True
        store = VectorStoreService(
            collection_name=STRUCTURED_TEST_COLLECTION_NAME,
            persist_directory=STRUCTURED_TEST_CHROMA_DIR,
        )
        lexical_path = (
            STRUCTURED_TEST_CHROMA_DIR.parent
            / "structured_test_bm25.json"
        )
    elif index_profile == "production":
        store = None
    else:
        raise ValueError(f"未知索引配置：{index_profile}")

    if not skip_sync:
        sync = ensure_rag_index_current(output=print, force=True)
        if not sync.succeeded:
            raise RuntimeError(f"知识库同步失败：{sync.message}")

    if store is None:
        store = VectorStoreService()
        from aa_my_agent.config import RAG_BM25_INDEX_PATH

        lexical_path = RAG_BM25_INDEX_PATH
    if index_profile == "production":
        # 与正式 RagService 使用同一个工厂，确保评测覆盖当前生产开关，
        # 包括可选的本地 Cross-Encoder 精排层。
        from aa_my_agent.rag.reranked_retriever import (
            create_default_rag_retriever,
        )

        retriever = create_default_rag_retriever(
            vector_store=store,
            max_distance=RAG_MAX_DISTANCE,
        )
    else:
        retriever = HybridRetriever(
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
    results: list[CaseResult] = []

    for case in cases:
        if not case.get("enabled", False):
            continue

        started = perf_counter()
        try:
            retrieval = retriever.retrieve(
                query=case["question"],
                final_k=top_k,
            )
            all_hits = [
                _to_hit(
                    hit.document,
                    hit.dense_distance
                    if hit.dense_distance is not None
                    else float("inf"),
                )
                for hit in retrieval.candidates[:top_k]
            ]
            accepted_hits = [
                _to_hit(
                    hit.document,
                    hit.dense_distance
                    if hit.dense_distance is not None
                    else float("inf"),
                )
                for hit in retrieval.hits
            ]
            passed, reason = score_case(case, accepted_hits)
        except Exception as exc:  # Keep the remaining exam cases running.
            all_hits = []
            passed = False
            reason = f"检索执行失败：{type(exc).__name__}: {exc}"

        results.append(
            CaseResult(
                case_id=case["id"],
                category=case["category"],
                question=case["question"],
                passed=passed,
                reason=reason,
                latency_seconds=perf_counter() - started,
                hits=tuple(all_hits),
            )
        )

        mark = "通过" if passed else "失败"
        print(f"[{mark}] {case['id']} - {reason}")

    return results, float(RAG_MAX_DISTANCE)


def _table_text(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_reports(
    results: list[CaseResult],
    cases_path: Path,
    top_k: int,
    max_distance: float,
    report_name: str,
    index_profile: str = "production",
) -> tuple[Path, Path]:
    """Write one readable Markdown report and one machine-readable JSON report."""
    DEFAULT_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(
        char for char in report_name if char.isalnum() or char in {"-", "_"}
    ).strip("-_")
    if not safe_name:
        raise ValueError("report-name 至少要包含一个字母、数字、横线或下划线")

    markdown_path = DEFAULT_REPORTS_DIR / f"{safe_name}.md"
    json_path = DEFAULT_REPORTS_DIR / f"{safe_name}.json"
    passed_count = sum(result.passed for result in results)
    total_latency = sum(result.latency_seconds for result in results)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    lines = [
        "# RAG 最小测试基线报告",
        "",
        f"- 生成时间：{generated_at}",
        f"- 题库：`{cases_path}`",
        f"- 索引配置：`{index_profile}`",
        f"- 判定范围：混合检索最终前 {top_k} 条；向量门槛 {max_distance:.2f}",
        f"- 总结果：{passed_count}/{len(results)} 通过",
        f"- 检索总耗时：{total_latency:.3f} 秒",
        "",
        "| ID | 类型 | 结果 | 耗时（秒） | 原因 |",
        "|---|---|---:|---:|---|",
    ]

    for result in results:
        lines.append(
            f"| {_table_text(result.case_id)} | {_table_text(result.category)} "
            f"| {'通过' if result.passed else '失败'} | {result.latency_seconds:.3f} "
            f"| {_table_text(result.reason)} |"
        )

    failed = [result for result in results if not result.passed]
    if failed:
        lines.extend(["", "## 失败详情", ""])
        for result in failed:
            lines.extend(
                [
                    f"### {result.case_id}",
                    "",
                    f"问题：{result.question}",
                    "",
                    f"原因：{result.reason}",
                    "",
                ]
            )
            if not result.hits:
                lines.append("候选检索结果：无")
                lines.append("")
                continue
            lines.append("前 K 条融合候选（可能包含未通过最终证据门槛的结果）：")
            lines.append("")
            for index, hit in enumerate(result.hits, start=1):
                page = f"第 {hit.page} 页" if hit.page is not None else "无页码"
                preview = " ".join(hit.content.split())[:180]
                lines.append(
                    f"{index}. `{hit.source}`，{page}，距离 {hit.distance:.6f}：{preview}"
                )
            lines.append("")

    markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    machine_report = {
        "generated_at": generated_at,
        "cases_path": str(cases_path),
        "top_k": top_k,
        "max_distance": max_distance,
        "index_profile": index_profile,
        "summary": {
            "total": len(results),
            "passed": passed_count,
            "failed": len(results) - passed_count,
            "pass_rate": passed_count / len(results) if results else 0.0,
            "latency_seconds": total_latency,
        },
        "results": [
            {
                "id": result.case_id,
                "category": result.category,
                "question": result.question,
                "passed": result.passed,
                "reason": result.reason,
                "latency_seconds": result.latency_seconds,
                "hits": [
                    {
                        "source": hit.source,
                        "page": hit.page,
                        "distance": hit.distance,
                        "content_preview": " ".join(hit.content.split())[:500],
                    }
                    for hit in result.hits
                ],
            }
            for result in results
        ],
    }
    json_path.write_text(
        json.dumps(machine_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return markdown_path, json_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 RAG 最小检索测试基线")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH, help="题库 JSON 路径")
    parser.add_argument("--top-k", type=int, default=5, help="每题最多检查多少条候选结果")
    parser.add_argument(
        "--report-name",
        default="baseline_before_upgrade",
        help="报告文件名（不含扩展名）",
    )
    parser.add_argument("--validate-only", action="store_true", help="只检查题库格式，不运行检索")
    parser.add_argument("--skip-sync", action="store_true", help="跳过运行前的知识库同步检查")
    parser.add_argument(
        "--index-profile",
        choices=("production", "structured-test"),
        default="production",
        help="选择正式索引或隔离的结构化测试索引",
    )
    parser.add_argument(
        "--only",
        help="只运行指定题目ID，多个ID用英文逗号分隔",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.top_k <= 0:
        print("错误：--top-k 必须是正整数", file=sys.stderr)
        return 2

    try:
        cases_path = args.cases.resolve()
        _payload, cases = load_cases(cases_path)
    except ValueError as exc:
        print(f"题库检查失败：{exc}", file=sys.stderr)
        return 2

    if args.only:
        selected_ids = {
            value.strip()
            for value in args.only.split(",")
            if value.strip()
        }
        known_ids = {str(case.get("id")) for case in cases}
        unknown_ids = sorted(selected_ids - known_ids)
        if unknown_ids:
            print(
                "错误：--only 包含未知题目ID：" + ", ".join(unknown_ids),
                file=sys.stderr,
            )
            return 2
        cases = [
            {
                **case,
                "enabled": bool(case.get("enabled", False))
                and str(case.get("id")) in selected_ids,
            }
            for case in cases
        ]

    enabled_count = sum(bool(case.get("enabled", False)) for case in cases)
    print(f"题库格式正确：共 {len(cases)} 题，已开启 {enabled_count} 题。")

    if args.validate_only:
        return 0
    if enabled_count == 0:
        print("还没有开启的题目。请先填写 cases.json，并把确认好的题目设为 enabled: true。")
        return 0

    try:
        if args.index_profile == "structured-test":
            print("使用隔离的结构化测试 Chroma；不会同步或修改正式索引。")
        results, max_distance = run_cases(
            cases,
            args.top_k,
            args.skip_sync,
            index_profile=args.index_profile,
        )
        markdown_path, json_path = write_reports(
            results=results,
            cases_path=cases_path,
            top_k=args.top_k,
            max_distance=max_distance,
            report_name=args.report_name,
            index_profile=args.index_profile,
        )
    except Exception as exc:
        print(f"测试无法完成：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"报告已生成：{markdown_path}")
    print(f"原始数据已生成：{json_path}")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
