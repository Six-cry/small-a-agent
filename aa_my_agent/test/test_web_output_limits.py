import json

from aa_my_agent.tools import definitions


def test_web_search_defaults_to_three_and_limits_each_snippet(monkeypatch):
    captured = {}

    def fake_post(endpoint, payload):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {
            "results": [{
                "title": "官方信息",
                "url": "https://example.com/source",
                "content": "资" * 2_000,
                "score": 1.0,
            }]
        }

    monkeypatch.setattr(definitions, "_tavily_post", fake_post)

    output = json.loads(definitions.run_web_search("杭州国庆安排"))

    assert captured["endpoint"] == "/search"
    assert captured["payload"]["max_results"] == 3
    assert len(output["results"][0]["content"]) == 1_200


def test_fetch_url_limits_page_body_to_ten_thousand_chars(monkeypatch):
    monkeypatch.setattr(
        definitions,
        "_tavily_post",
        lambda _endpoint, _payload: {
            "results": [{"raw_content": "页" * 20_000}]
        },
    )

    output = definitions.run_fetch_url("https://example.com/page")

    assert "页" * 10_000 in output
    assert "页" * 10_001 not in output
    assert "page content truncated" in output


def test_old_larger_max_results_is_clamped_without_error(monkeypatch):
    captured = {}

    def fake_post(_endpoint, payload):
        captured["max_results"] = payload["max_results"]
        return {"results": [{
            "title": "结果",
            "url": "https://example.com",
            "content": "摘要",
        }]}

    monkeypatch.setattr(definitions, "_tavily_post", fake_post)

    output = definitions.run_web_search("杭州", max_results=5)

    assert not output.startswith("Error:")
    assert captured["max_results"] == 3
