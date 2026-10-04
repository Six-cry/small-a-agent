"""Read-only, compact hourly forecasts from the official Open-Meteo APIs."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone

import requests

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_FIELDS = (
    "temperature_2m",
    "precipitation_probability",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
)
MAX_DAYS = 7
MAX_RESPONSE_BYTES = 300_000

server = MCPServer(
    "small-a-hourly-weather",
    description="Read-only Open-Meteo hourly weather for dated travel plans.",
)


def _request_json(base_url: str, parameters: dict[str, object]) -> dict[str, object]:
    try:
        with requests.get(
            base_url,
            params=parameters,
            headers={"User-Agent": "small-a-hourly-weather/1.0"},
            timeout=10,
            stream=True,
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            total_bytes = 0
            for chunk in response.iter_content(chunk_size=8192):
                total_bytes += len(chunk)
                if total_bytes > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Open-Meteo response was unexpectedly large")
                chunks.append(chunk)
            raw = b"".join(chunks)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise RuntimeError(f"Open-Meteo HTTP error {status}") from exc
    except requests.RequestException as exc:
        raise RuntimeError("Open-Meteo connection failed; check network/TLS and retry") from exc

    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Open-Meteo returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Open-Meteo returned an unexpected response")
    if payload.get("error"):
        reason = str(payload.get("reason", "unknown error"))[:200]
        raise RuntimeError(f"Open-Meteo error: {reason}")
    return payload


def _parse_dates(start_date: str, end_date: str) -> tuple[date, date]:
    if not isinstance(start_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date):
        raise ValueError("Dates must use YYYY-MM-DD format")
    if end_date and (not isinstance(end_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_date)):
        raise ValueError("Dates must use YYYY-MM-DD format")
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date or start_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("Dates must use YYYY-MM-DD format") from exc
    if end < start:
        raise ValueError("end_date must not precede start_date")
    if (end - start).days >= MAX_DAYS:
        raise ValueError(f"Request no more than {MAX_DAYS} inclusive forecast days")
    return start, end


def _location(city: str, country_code: str) -> dict[str, object]:
    parameters: dict[str, object] = {
        "name": city,
        "count": 1,
        "language": "zh",
        "format": "json",
    }
    if country_code:
        parameters["countryCode"] = country_code
    locations = _request_json(GEOCODING_URL, parameters).get("results")
    if not isinstance(locations, list) or not locations or not isinstance(locations[0], dict):
        raise ValueError(f"Open-Meteo could not find city: {city}")
    location = locations[0]
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    time_zone = location.get("timezone")
    if (
        not isinstance(latitude, (int, float))
        or not isinstance(longitude, (int, float))
        or not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
        or not isinstance(time_zone, str)
        or not time_zone
    ):
        raise RuntimeError("Open-Meteo geocoding returned incomplete coordinates or timezone")
    return location


def _build_hourly_forecast(
    city: str,
    start_date: str,
    end_date: str = "",
    country_code: str = "",
    start_hour: int = 0,
    end_hour: int = 23,
) -> dict[str, object]:
    """Return hourly forecasts; each row follows the response's columns list."""
    city = city.strip()
    if len(city) < 2 or len(city) > 100:
        raise ValueError("city must contain 2-100 characters")
    if country_code and (len(country_code) != 2 or not country_code.isalpha()):
        raise ValueError("country_code must be an ISO 3166-1 two-letter code")
    if not isinstance(start_hour, int) or not isinstance(end_hour, int):
        raise ValueError("start_hour and end_hour must be integers")
    if not 0 <= start_hour <= end_hour <= 23:
        raise ValueError("Hours must satisfy 0 <= start_hour <= end_hour <= 23")
    start, end = _parse_dates(start_date, end_date)
    location = _location(city, country_code.upper())

    forecast = _request_json(
        FORECAST_URL,
        {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "timezone": location["timezone"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": ",".join(HOURLY_FIELDS),
        },
    )
    hourly = forecast.get("hourly")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise RuntimeError("Open-Meteo forecast has no hourly data")
    times = hourly["time"]
    if not all(isinstance(hourly.get(field), list) and len(hourly[field]) == len(times)
               for field in HOURLY_FIELDS):
        raise RuntimeError("Open-Meteo hourly columns are missing or misaligned")

    rows: list[list[object]] = []
    for index, local_time in enumerate(times):
        if not isinstance(local_time, str):
            raise RuntimeError("Open-Meteo returned an invalid hourly timestamp")
        try:
            hour = datetime.fromisoformat(local_time).hour
        except ValueError as exc:
            raise RuntimeError("Open-Meteo returned an invalid hourly timestamp") from exc
        if start_hour <= hour <= end_hour:
            rows.append([local_time, *(hourly[field][index] for field in HOURLY_FIELDS)])

    if not rows or all(all(value is None for value in row[1:]) for row in rows):
        raise ValueError("No forecast is available for these dates; check the 16-day horizon")
    units = forecast.get("hourly_units")
    if not isinstance(units, dict):
        units = {}
    return {
        "source": "Open-Meteo Forecast API",
        "source_documentation": "https://open-meteo.com/en/docs",
        "attribution": "Weather data by Open-Meteo.com, CC BY 4.0",
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "location": {
            "name": location.get("name"),
            "region": location.get("admin1"),
            "country": location.get("country"),
            "latitude": location["latitude"],
            "longitude": location["longitude"],
        },
        "timezone": forecast.get("timezone", location["timezone"]),
        "date_range": [start.isoformat(), end.isoformat()],
        "hour_range_local": [start_hour, end_hour],
        "columns": ["local_time", *HOURLY_FIELDS],
        "units": {field: units.get(field) for field in HOURLY_FIELDS},
        "hours": rows,
        "precipitation_note": (
            "Probability is for more than 0.1 mm in the preceding hour; "
            "precipitation amount is accumulated over the preceding hour."
        ),
    }


@server.tool(
    description=(
        "Get hourly weather for a confirmed trip date or up to 7 inclusive dates. "
        "Use YYYY-MM-DD dates within Open-Meteo's 16-day forecast horizon. "
        "Returns local-time temperature, precipitation probability/amount, "
        "weather code and wind speed in a compact table. No booking data."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def get_hourly_forecast(
    city: str,
    start_date: str,
    end_date: str = "",
    country_code: str = "",
    start_hour: int = 0,
    end_hour: int = 23,
) -> dict[str, object]:
    """Expose expected input/provider failures without an MCP server traceback."""
    try:
        return _build_hourly_forecast(
            city, start_date, end_date, country_code, start_hour, end_hour
        )
    except (ValueError, RuntimeError) as exc:
        raise ToolError(str(exc)) from None


if __name__ == "__main__":
    server.run("stdio")
