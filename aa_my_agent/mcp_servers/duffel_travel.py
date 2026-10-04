"""Read-only flight and accommodation searches backed by Duffel.

The provider uses POST to create *searches*. This server exposes no order,
booking, payment, cancellation, or account mutation operation.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import date
from urllib.parse import urlencode

import requests

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


DUFFEL_API = "https://api.duffel.com"
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
MAX_RESPONSE_BYTES = 12_000_000
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,  # Duffel creates a new search resource per request.
    openWorldHint=True,
)

server = MCPServer(
    "small-a-duffel-travel",
    description="Flight offers and accommodation availability. Search only; no purchases.",
)


class TravelLookupError(ToolError):
    """An error message that is safe to return to the agent."""


def _http_json(url: str, *, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Duffel-Version": "v2",
            }
        )
    try:
        with requests.request(
            "POST" if body else "GET",
            url,
            headers=headers,
            json=body,
            timeout=(3, 15),
            stream=True,
            allow_redirects=False,
        ) as response:
            status = response.status_code
            if status not in (200, 201):
                if status == 401:
                    raise TravelLookupError("Duffel credential was rejected (HTTP 401).")
                if status == 403:
                    raise TravelLookupError(
                        "Duffel denied this product (HTTP 403). Check Flights/Stays access."
                    )
                if status == 429:
                    raise TravelLookupError("Provider rate limit reached (HTTP 429).")
                raise TravelLookupError(f"Provider request failed (HTTP {status}).")
            payload = bytearray()
            for chunk in response.iter_content(chunk_size=65_536):
                payload.extend(chunk)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise TravelLookupError(
                        "Provider response is too large; narrow the search area."
                    )
    except requests.exceptions.RequestException:
        raise TravelLookupError("Travel provider is temporarily unreachable.") from None
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TravelLookupError("Provider returned invalid JSON.") from None
    if not isinstance(parsed, dict):
        raise TravelLookupError("Provider returned an unexpected response.")
    return parsed


def _token() -> str:
    token = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
    if not token:
        raise TravelLookupError("DUFFEL_ACCESS_TOKEN is not configured.")
    return token


def _mode(token: str, live_mode: object = None) -> str:
    if isinstance(live_mode, bool):
        return "live" if live_mode else "test"
    if token.startswith("duffel_test_"):
        return "test"
    if token.startswith("duffel_live_"):
        return "live"
    return "unverified"


def _date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise TravelLookupError(f"{label} must use YYYY-MM-DD.") from None
    if parsed < date.today():
        raise TravelLookupError(f"{label} cannot be in the past.")
    return parsed


def _positive_int(value: int, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise TravelLookupError(f"{label} must be an integer from 1 to {maximum}.")
    return value


def _iata(value: str, label: str) -> str:
    code = value.strip().upper() if isinstance(value, str) else ""
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise TravelLookupError(f"{label} must be a three-letter IATA airport/city code.")
    return code


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _pick(value: object, *keys: str) -> dict:
    source = _object(value)
    return {key: source[key] for key in keys if key in source and source[key] is not None}


def _flight_place(value: object) -> dict:
    return _pick(value, "iata_code", "name", "city_name")


def _flight_offer(value: object) -> dict:
    offer = _object(value)
    result = _pick(
        offer,
        "id",
        "total_amount",
        "total_currency",
        "expires_at",
        "total_duration",
    )
    result["airline"] = _pick(offer.get("owner"), "name", "iata_code")
    slices = []
    for item in _list(offer.get("slices"))[:2]:
        slice_data = _object(item)
        compact_slice = _pick(slice_data, "duration")
        compact_slice["origin"] = _flight_place(slice_data.get("origin"))
        compact_slice["destination"] = _flight_place(slice_data.get("destination"))
        segments = []
        for item_segment in _list(slice_data.get("segments"))[:8]:
            segment = _object(item_segment)
            compact_segment = _pick(
                segment,
                "departing_at",
                "arriving_at",
                "duration",
                "marketing_carrier_flight_number",
            )
            compact_segment["origin"] = _flight_place(segment.get("origin"))
            compact_segment["destination"] = _flight_place(segment.get("destination"))
            compact_segment["operating_carrier"] = _pick(
                segment.get("operating_carrier"), "name", "iata_code"
            )
            segments.append(compact_segment)
        compact_slice["segments"] = segments
        if len(_list(slice_data.get("segments"))) > len(segments):
            compact_slice["segments_omitted"] = len(slice_data["segments"]) - len(segments)
        slices.append(compact_slice)
    result["slices"] = slices
    return result


def _hotel_result(value: object) -> dict:
    result = _object(value)
    accommodation = _object(result.get("accommodation"))
    location = _object(accommodation.get("location"))
    compact = _pick(
        result,
        "id",
        "cheapest_rate_total_amount",
        "cheapest_rate_currency",
        "expires_at",
    )
    compact["accommodation"] = {
        **_pick(accommodation, "id", "name", "rating", "review_score", "review_count"),
        "address": _pick(
            location.get("address"),
            "line_one",
            "city_name",
            "region",
            "country_code",
        ),
    }
    return compact


def _geocode_city(city: str, country_code: str | None) -> dict:
    if not isinstance(city, str) or len(city.strip()) < 2 or len(city) > 120:
        raise TravelLookupError("city must contain 2–120 characters.")
    params = {"name": city.strip(), "count": 3, "language": "zh", "format": "json"}
    if country_code:
        country = country_code.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise TravelLookupError("country_code must be a two-letter country code.")
        params["countryCode"] = country
    response = _http_json(f"{GEOCODING_API}?{urlencode(params)}")
    places = _list(response.get("results"))
    if not places:
        raise TravelLookupError("City was not found; specify a country or WGS84 coordinates.")
    place = _object(places[0])
    if not isinstance(place.get("latitude"), (float, int)) or not isinstance(
        place.get("longitude"), (float, int)
    ):
        raise TravelLookupError("Geocoding response has no usable coordinates.")
    return _pick(place, "name", "country", "admin1", "latitude", "longitude")


@server.tool(
    description=(
        "Search Duffel flight offers by IATA airport/city codes and date. Returns up to "
        "10 lowest-priced offers with flight times and airlines. Search only: never "
        "books or pays. Test-mode offers are simulated and are not real prices."
    ),
    annotations=READ_ONLY,
)
def search_flight_offers(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str | None = None,
    adults: int = 1,
    cabin_class: str = "economy",
    max_results: int = 5,
) -> dict[str, object]:
    try:
        origin_code = _iata(origin, "origin")
        destination_code = _iata(destination, "destination")
        if origin_code == destination_code:
            raise TravelLookupError("origin and destination must differ.")
        departure = _date(departure_date, "departure_date")
        slices = [
            {
                "origin": origin_code,
                "destination": destination_code,
                "departure_date": departure.isoformat(),
            }
        ]
        if return_date:
            returning = _date(return_date, "return_date")
            if returning <= departure:
                raise TravelLookupError("return_date must be after departure_date.")
            slices.append(
                {
                    "origin": destination_code,
                    "destination": origin_code,
                    "departure_date": returning.isoformat(),
                }
            )
        count = _positive_int(adults, "adults", maximum=9)
        limit = _positive_int(max_results, "max_results", maximum=10)
        if cabin_class not in {"economy", "premium_economy", "business", "first"}:
            raise TravelLookupError("cabin_class is not supported.")
        token = _token()
        request_data = {
            "slices": slices,
            "passengers": [{"type": "adult"} for _ in range(count)],
            "cabin_class": cabin_class,
        }
        created = _http_json(
            f"{DUFFEL_API}/air/offer_requests?return_offers=false&supplier_timeout=10000",
            token=token,
            body={"data": request_data},
        )
        search = _object(created.get("data"))
        search_id = search.get("id")
        if not isinstance(search_id, str) or not search_id.startswith("orq_"):
            raise TravelLookupError("Duffel returned an invalid flight search response.")
        query = urlencode(
            {"offer_request_id": search_id, "limit": limit, "sort": "total_amount"}
        )
        listed = _http_json(f"{DUFFEL_API}/air/offers?{query}", token=token)
        offers = listed.get("data")
        if not isinstance(offers, list):
            raise TravelLookupError("Duffel returned an invalid flight offers response.")
        mode = _mode(token, search.get("live_mode"))
        return {
            "ok": True,
            "source": "Duffel Flights",
            "mode": mode,
            "test_data": mode == "test",
            "search_id": search_id,
            "requested_slices": slices,
            "offers": [_flight_offer(item) for item in offers[:limit]],
            "shown_count": min(len(offers), limit),
            "note": "Prices and availability can change; recheck the offer before booking.",
        }
    except TravelLookupError:
        raise  # MCP returns is_error=true; a failed lookup must not enter the turn cache.


@server.tool(
    description=(
        "Search Duffel Stays hotel availability and cheapest total price for dates, "
        "guests, and a city or explicit WGS84 coordinates. Coordinates from AMap "
        "are GCJ-02 and must not be passed directly. Search only; no bookings. "
        "Duffel Stays product access must be granted separately. Test prices are simulated."
    ),
    annotations=READ_ONLY,
)
def search_available_stays(
    check_in_date: str,
    check_out_date: str,
    city: str | None = None,
    country_code: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    adults: int = 1,
    children_ages: list[int] | None = None,
    rooms: int = 1,
    radius_km: int = 5,
    free_cancellation_only: bool = False,
    max_results: int = 5,
) -> dict[str, object]:
    try:
        check_in = _date(check_in_date, "check_in_date")
        check_out = _date(check_out_date, "check_out_date")
        nights = (check_out - check_in).days
        if not 1 <= nights <= 99:
            raise TravelLookupError("Stay must be between 1 and 99 nights.")
        if (check_in - date.today()).days > 330:
            raise TravelLookupError("check_in_date cannot be more than 330 days ahead.")
        adult_count = _positive_int(adults, "adults", maximum=20)
        room_count = _positive_int(rooms, "rooms", maximum=20)
        if adult_count < room_count:
            raise TravelLookupError("At least one adult is needed per room.")
        radius = _positive_int(radius_km, "radius_km", maximum=100)
        limit = _positive_int(max_results, "max_results", maximum=10)
        ages = children_ages or []
        if not isinstance(ages, list) or len(ages) > 10 or any(
            isinstance(age, bool) or not isinstance(age, int) or not 0 <= age <= 17
            for age in ages
        ):
            raise TravelLookupError("children_ages must contain up to 10 ages from 0 to 17.")
        if city and (latitude is not None or longitude is not None):
            raise TravelLookupError("Specify either city or WGS84 coordinates, not both.")
        token = _token()
        if city:
            selected = _geocode_city(city, country_code)
            coordinate_source = "Open-Meteo Geocoding (WGS84)"
        else:
            if latitude is None or longitude is None:
                raise TravelLookupError("Provide city or both WGS84 latitude and longitude.")
            if not all(
                isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
                for value in (latitude, longitude)
            ) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise TravelLookupError("WGS84 coordinates are outside valid ranges.")
            selected = {"latitude": float(latitude), "longitude": float(longitude)}
            coordinate_source = "caller-supplied WGS84"
        data = {
            "check_in_date": check_in.isoformat(),
            "check_out_date": check_out.isoformat(),
            "rooms": room_count,
            "guests": [{"type": "adult"} for _ in range(adult_count)]
            + [{"type": "child", "age": age} for age in ages],
            "free_cancellation_only": bool(free_cancellation_only),
            "location": {
                "radius": radius,
                "geographic_coordinates": {
                    "latitude": selected["latitude"],
                    "longitude": selected["longitude"],
                },
            },
        }
        response = _http_json(f"{DUFFEL_API}/stays/search", token=token, body={"data": data})
        search = _object(response.get("data"))
        results = search.get("results")
        if not isinstance(results, list):
            raise TravelLookupError("Duffel returned an invalid hotel search response.")
        mode = _mode(token, search.get("live_mode"))
        return {
            "ok": True,
            "source": "Duffel Stays",
            "mode": mode,
            "test_data": mode == "test",
            "search_location": selected,
            "coordinate_source": coordinate_source,
            "free_cancellation_filter": bool(free_cancellation_only),
            "hotels": [_hotel_result(item) for item in results[:limit]],
            "result_count": len(results),
            "shown_count": min(len(results), limit),
            "note": (
                "Search price is an initial quote; room details and cancellation terms "
                "must be checked before booking."
            ),
        }
    except TravelLookupError:
        raise  # MCP returns is_error=true; a failed lookup must not enter the turn cache.


if __name__ == "__main__":
    server.run("stdio")
