"""Tavily 熔断器的离线单元测试，不发送真实网络请求。"""

import json
import unittest

from aa_my_agent.tools.web_circuit import TavilyCircuitBreaker


SSL_ERROR = (
    "Error: web search failed - RuntimeError: "
    "Tavily network error: "
    "[SSL: UNEXPECTED_EOF_WHILE_READING]"
)


class TavilyCircuitBreakerTests(unittest.TestCase):
    def test_same_transport_error_opens_after_threshold(self):
        circuit = TavilyCircuitBreaker(threshold=2)

        _output, first_opened = circuit.observe("web_search", SSL_ERROR)
        _output, second_opened = circuit.observe("fetch_url", SSL_ERROR)

        self.assertFalse(first_opened)
        self.assertTrue(second_opened)
        self.assertTrue(circuit.is_open)
        self.assertEqual(circuit.last_error, "ssl_unexpected_eof")

    def test_success_resets_consecutive_error_count(self):
        circuit = TavilyCircuitBreaker(threshold=2)

        circuit.observe("web_search", SSL_ERROR)
        circuit.observe("web_search", '{"results": []}')
        circuit.observe("web_search", SSL_ERROR)

        self.assertFalse(circuit.is_open)
        self.assertEqual(circuit.same_error_count, 1)

    def test_open_circuit_only_hides_tavily_tools(self):
        circuit = TavilyCircuitBreaker(threshold=2)
        circuit.force_open("test")
        tools = [
            {"name": "web_search"},
            {"name": "fetch_url"},
            {"name": "search_knowledge"},
            {"name": "get_current_time"},
        ]

        available = circuit.available_tools(tools)

        self.assertEqual(
            [tool["name"] for tool in available],
            ["search_knowledge", "get_current_time"],
        )

    def test_parent_adopts_subagent_circuit_state(self):
        circuit = TavilyCircuitBreaker(threshold=2)
        report = json.dumps(
            {
                "status": "completed",
                "outcome": "degraded",
                "failed_tool_calls": 2,
                "web_circuit_open": True,
                "web_error": "ssl_unexpected_eof",
            }
        )

        parsed, opened = circuit.adopt_subagent_result(
            "subagent_task",
            report,
        )

        self.assertTrue(opened)
        self.assertTrue(circuit.is_open)
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(circuit.last_error, "ssl_unexpected_eof")


if __name__ == "__main__":
    unittest.main()
