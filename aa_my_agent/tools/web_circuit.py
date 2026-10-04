from dataclasses import dataclass
import json
import re


TAVILY_TOOL_NAMES = frozenset({
    "web_search",
    "fetch_url",
})


def tavily_error_fingerprint(
    output: str,
) -> str | None:
    """识别会影响整个Tavily通道的错误。"""
    text = str(output).casefold()

    if "unexpected_eof_while_reading" in text:
        return "ssl_unexpected_eof"

    if "tavily network error" in text:
        return "tavily_network_error"

    if "tavily request timed out" in text:
        return "tavily_timeout"

    match = re.search(
        r"tavily returned http\s+(\d+)",
        text,
    )

    if match:
        status_code = int(match.group(1))

        if status_code in {
            408,
            429,
            500,
            502,
            503,
            504,
            529,
        }:
            return f"tavily_http_{status_code}"

        if status_code in {401, 403}:
            return f"tavily_auth_{status_code}"

    return None


@dataclass
class TavilyCircuitBreaker:
    threshold: int = 2
    same_error_count: int = 0
    last_error: str | None = None
    is_open: bool = False

    def available_tools(self, tools: list[dict]) -> list[dict]:
        """熔断后只隐藏共享同一Tavily通道的工具。"""
        if not self.is_open:
            return tools

        return [
            tool
            for tool in tools
            if tool.get("name") not in TAVILY_TOOL_NAMES
        ]

    def blocks(self, tool_name: str) -> bool:
        """判断当前调用是否应被已打开的熔断器拦截。"""
        return self.is_open and tool_name in TAVILY_TOOL_NAMES

    def record(
        self,
        tool_name: str,
        output: str,
    ) -> bool:
        """
        记录工具执行结果。

        返回True表示熔断器在本次调用后刚刚打开。
        """
        if tool_name not in TAVILY_TOOL_NAMES:
            return False

        fingerprint = tavily_error_fingerprint(
            output
        )

        # 没有基础设施错误。
        if fingerprint is None:
            # 如果工具成功，重置连续错误。
            if not str(output).lstrip().casefold().startswith(
                "error:"
            ):
                self.same_error_count = 0
                self.last_error = None

            return False

        if fingerprint == self.last_error:
            self.same_error_count += 1
        else:
            self.last_error = fingerprint
            self.same_error_count = 1

        # Key或权限错误不需要重复尝试。
        if fingerprint.startswith(
            "tavily_auth_"
        ):
            self.same_error_count = self.threshold

        if (
            not self.is_open
            and self.same_error_count
            >= self.threshold
        ):
            self.is_open = True
            return True

        return False

    def force_open(
        self,
        reason: str,
    ) -> None:
        """将Subagent的熔断状态传给主Agent。"""
        self.is_open = True
        self.last_error = reason
        self.same_error_count = max(
            self.same_error_count,
            self.threshold,
        )

    def observe(
        self,
        tool_name: str,
        output: str,
    ) -> tuple[str, bool]:
        """记录真实工具结果，并在刚熔断时附加一次运行时说明。"""
        opened = self.record(tool_name, output)
        output_text = str(output)

        if opened:
            output_text += (
                "\n\n[Runtime notice] "
                "Tavily web_search and fetch_url are disabled "
                "for the remainder of this agent run. Continue "
                "with other available tools and clearly mark "
                "current facts that could not be verified."
            )

        return output_text, opened

    def adopt_subagent_result(
        self,
        tool_name: str,
        output: str,
    ) -> tuple[dict | None, bool]:
        """读取Subagent报告，并把其Tavily熔断状态传播给父Agent。"""
        if tool_name != "subagent_task":
            return None, False

        try:
            report = json.loads(str(output))
        except (json.JSONDecodeError, TypeError):
            return None, False

        if not isinstance(report, dict):
            return None, False

        opened = False
        if report.get("web_circuit_open") and not self.is_open:
            self.force_open(
                str(report.get("web_error") or "subagent_tavily_failure")
            )
            opened = True

        return report, opened

    def blocked_output(self) -> str:
        """返回模型可以理解的熔断信息。"""
        return (
            "Error: TAVILY_CIRCUIT_OPEN "
            + json.dumps(
                {
                    "error_code":
                        "TAVILY_CIRCUIT_OPEN",
                    "reason": self.last_error,
                    "same_error_count":
                        self.same_error_count,
                    "message": (
                        "Tavily is unavailable "
                        "for the current agent turn."
                    ),
                },
                ensure_ascii=False,
            )
        )
