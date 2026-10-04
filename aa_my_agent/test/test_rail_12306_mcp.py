from datetime import date, timedelta

import pytest

from aa_my_agent.mcp_servers.rail_12306 import get_12306_official_lookup


def test_rail_lookup_points_to_12306_without_claiming_live_inventory():
    travel_date = (date.today() + timedelta(days=5)).isoformat()
    result = get_12306_official_lookup("南京南", "上海虹桥", travel_date)

    assert result["query"]["origin"] == "南京南"
    assert result["query"]["departure_date"] == travel_date
    assert result["live_data"] is False
    assert result["official_ticket_lookup_url"].startswith("https://kyfw.12306.cn/")
    assert "实时余票" in result["instruction"]


def test_rail_lookup_rejects_invalid_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        get_12306_official_lookup("南京南", "上海虹桥", "明天")
