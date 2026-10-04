"""主 Agent 中研究型 Subagent 的合并与复用管理。"""

from dataclasses import dataclass, field
import json
from types import SimpleNamespace

from ..telemetry import emit


def is_research_subagent(block) -> bool:
    """判断工具调用是否为只读研究型 Subagent。"""
    return (
        getattr(block, "name", None) == "subagent_task"
        and str(getattr(block, "input", {}).get("mode", "research"))
        == "research"
    )


def research_scope(instructions: str) -> frozenset[str]:
    """粗粒度标记研究覆盖范围，用于安全复用同回合报告。"""
    text = str(instructions).casefold()
    scope = set()
    if any(marker in text for marker in (
        "web", "网页", "联网", "时效", "最新", "官方",
        "票价", "班次", "预约", "开放时间",
    )):
        scope.add("web")
    if any(marker in text for marker in (
        "rag", "知识库", "本地", "pdf", "文档",
    )):
        scope.add("rag")
    return frozenset(scope or {"general"})


def successful_subagent_report(output: str) -> bool:
    """判断Subagent JSON报告是否明确完成了研究目标。"""
    try:
        report = json.loads(str(output))
    except (TypeError, ValueError):
        return False
    return (
        isinstance(report, dict)
        and report.get("status") == "completed"
        and report.get("outcome") == "success"
        and report.get("objective_met") is True
    )


def _combined_research_block(blocks: list) -> SimpleNamespace:
    """把同一响应中的多个只读研究委托合并为一次执行。"""
    leader = blocks[0]
    tasks = []
    for index, block in enumerate(blocks, start=1):
        instructions = str(block.input.get("instructions", "")).strip()
        if instructions:
            tasks.append(f"## Research task {index}\n{instructions}")
    combined = (
        "Complete the following related research tasks in one run. "
        "Share evidence across tasks, avoid duplicate searches, and return "
        "one concise integrated report with source URLs.\n\n"
        + "\n\n".join(tasks)
    )
    return SimpleNamespace(
        type="tool_use",
        id=leader.id,
        name=leader.name,
        input={"instructions": combined, "mode": "research"},
    )


@dataclass
class ResearchBatch:
    """同一次模型响应中的研究任务合并状态。"""

    leader_id: str | None = None
    follower_ids: set[str] = field(default_factory=set)
    execution_override: object | None = None
    report: str | None = None


@dataclass
class ResearchResolution:
    """一个工具调用经过研究复用判断后的执行方式。"""

    execution_block: object
    reused_output: str | None = None

    @property
    def reused(self) -> bool:
        return self.reused_output is not None


class ResearchManager:
    """管理单次主Agent运行中的Subagent批处理和报告复用。"""

    def __init__(self) -> None:
        self._reports: list[tuple[frozenset[str], str, str]] = []

    def prepare_batch(self, content: list, turn_id: str) -> ResearchBatch:
        """识别同一响应中的多个研究任务，并在需要时合并。"""
        research_blocks = [
            block for block in content if is_research_subagent(block)
        ]
        if len(research_blocks) <= 1:
            return ResearchBatch()

        leader_id = research_blocks[0].id
        batch = ResearchBatch(
            leader_id=leader_id,
            follower_ids={block.id for block in research_blocks[1:]},
            execution_override=_combined_research_block(research_blocks),
        )
        emit(
            "AGENT", "SubagentBatch", color="\033[35m",
            turn_id=turn_id,
            merged=len(research_blocks),
            leader=leader_id,
        )
        return batch

    def resolve(
        self,
        block,
        batch: ResearchBatch,
        turn_id: str,
    ) -> ResearchResolution:
        """决定执行原调用、合并调用，还是直接复用已有报告。"""
        if block.id in batch.follower_ids:
            batch_success = successful_subagent_report(batch.report or "")
            output = json.dumps(
                {
                    "status": "reused",
                    "outcome": "success" if batch_success else "degraded",
                    "objective_met": batch_success,
                    "reason": (
                        "batched_research"
                        if batch_success
                        else "batched_source_not_successful"
                    ),
                    "source_tool_use_id": batch.leader_id,
                    "output": (
                        "This request was merged into the research report "
                        "returned for the source tool call in this same "
                        "tool-result batch."
                    ),
                },
                ensure_ascii=False,
            )
            emit(
                "AGENT", "SubagentReuse", color="\033[35m",
                turn_id=turn_id,
                action="batched-result",
                source=batch.leader_id,
                target=block.id,
            )
            return ResearchResolution(block, reused_output=output)

        if is_research_subagent(block):
            requested_scope = research_scope(
                block.input.get("instructions", "")
            )
            for cached_scope, cached_output, cached_id in reversed(
                self._reports
            ):
                if requested_scope.issubset(cached_scope):
                    emit(
                        "AGENT", "SubagentReuse", color="\033[35m",
                        turn_id=turn_id,
                        action="cached-report",
                        source=cached_id,
                        target=block.id,
                        research_scope=",".join(sorted(requested_scope)),
                    )
                    return ResearchResolution(
                        block,
                        reused_output=cached_output,
                    )

        execution_block = (
            batch.execution_override
            if block.id == batch.leader_id
            else block
        )
        return ResearchResolution(execution_block)

    def observe(
        self,
        original_block,
        execution_block,
        output: str,
        batch: ResearchBatch,
    ) -> None:
        """记录已执行的研究报告，供本轮后续委托复用。"""
        if original_block.id == batch.leader_id:
            batch.report = output

        if (
            is_research_subagent(execution_block)
            and successful_subagent_report(output)
        ):
            scope = research_scope(
                execution_block.input.get("instructions", "")
            )
            self._reports.append((scope, output, original_block.id))
