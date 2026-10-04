"""Provider-aware MCP result reduction before content reaches the model."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AMAP_ROUTE_TOOLS = {
    "maps_bicycling",
    "maps_direction_driving",
    "maps_direction_transit_integrated",
    "maps_direction_walking",
}
AMAP_REDUCTION_THRESHOLD = 6_000
AMAP_MAX_ROUTE_OPTIONS = 3


@dataclass(frozen=True)
class MCPBodyReduction:
    body: str
    reduced: bool = False
    original_chars: int = 0
    raw_result_id: str | None = None
    raw_result_path: Path | None = None


def _pick(source: object, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {
        field: source[field]
        for field in fields
        if field in source and source[field] not in (None, "", [], {})
    }


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _compact_stop(value: object) -> object:
    if not isinstance(value, dict):
        return value
    return _pick(value, ("id", "name", "location"))


def _compact_steps(value: object, *, limit: int = 24) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for step in _as_list(value)[:limit]:
        kept = _pick(
            step,
            (
                "instruction",
                "road",
                "distance",
                "duration",
                "orientation",
                "action",
                "assistant_action",
                "toll",
            ),
        )
        if kept:
            compacted.append(kept)
    return compacted


def _compact_walking(value: object) -> dict[str, Any]:
    compacted = _pick(
        value,
        ("origin", "destination", "distance", "duration"),
    )
    if isinstance(value, dict):
        source_steps = _as_list(value.get("steps"))
        # Transit planning needs the shape of the transfer walk, not every
        # turn-by-turn geometry record. Keep both ends plus representative
        # middle instructions. Standalone walking routes use _compact_steps
        # below and retain up to 24 full navigation steps.
        selected_steps = (
            source_steps
            if len(source_steps) <= 5
            else [*source_steps[:4], source_steps[-1]]
        )
        steps = [
            _pick(step, ("instruction", "distance", "duration"))
            for step in selected_steps
        ]
        steps = [step for step in steps if step]
        if steps:
            compacted["steps"] = steps
            compacted["steps_returned"] = len(source_steps)
            compacted["steps_included"] = len(steps)
    return compacted


def _compact_bus(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    lines: list[dict[str, Any]] = []
    for line in _as_list(value.get("buslines"))[:2]:
        compacted = _pick(
            line,
            (
                "name",
                "id",
                "type",
                "distance",
                "duration",
                "start_time",
                "end_time",
                "via_num",
            ),
        )
        if isinstance(line, dict):
            for field in ("departure_stop", "arrival_stop"):
                if field in line:
                    compacted[field] = _compact_stop(line[field])
            via_stops = [
                _compact_stop(stop)
                for stop in _as_list(line.get("via_stops"))[:10]
            ]
            if via_stops:
                compacted["via_stops"] = via_stops
        if compacted:
            lines.append(compacted)
    return {"buslines": lines} if lines else {}


def _compact_railway(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    compacted = _pick(
        value,
        ("id", "name", "time", "distance", "trip", "type"),
    )
    for field in ("departure_stop", "arrival_stop"):
        if field in value:
            compacted[field] = _compact_stop(value[field])
    via_stops = [
        _compact_stop(stop)
        for stop in _as_list(value.get("via_stops"))[:10]
    ]
    if via_stops:
        compacted["via_stops"] = via_stops
    return compacted


def _compact_transit_segment(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compacted: dict[str, Any] = {}
    walking = _compact_walking(value.get("walking"))
    bus = _compact_bus(value.get("bus"))
    railway = _compact_railway(value.get("railway"))
    if walking:
        compacted["walking"] = walking
    if bus:
        compacted["bus"] = bus
    if railway:
        compacted["railway"] = railway
    for field in ("entrance", "exit"):
        stop = _compact_stop(value.get(field))
        if stop not in (None, "", [], {}):
            compacted[field] = stop
    taxi = _pick(value.get("taxi"), ("distance", "duration", "price"))
    if taxi:
        compacted["taxi"] = taxi
    return compacted


def _compact_transit_route(route: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    compacted = _pick(
        route,
        ("origin", "destination", "distance", "taxi_cost"),
    )
    source_options = _as_list(route.get("transits"))
    options: list[dict[str, Any]] = []
    for transit in source_options[:AMAP_MAX_ROUTE_OPTIONS]:
        option = _pick(
            transit,
            ("duration", "walking_distance", "distance", "cost", "nightflag"),
        )
        if isinstance(transit, dict):
            segments = [
                _compact_transit_segment(segment)
                for segment in _as_list(transit.get("segments"))
            ]
            segments = [segment for segment in segments if segment]
            if segments:
                option["segments"] = segments
        if option:
            options.append(option)
    compacted["transits"] = options
    return compacted, len(source_options), len(options)


def _compact_path_route(route: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    compacted = _pick(
        route,
        ("origin", "destination", "distance", "taxi_cost"),
    )
    source_options = _as_list(route.get("paths"))
    paths: list[dict[str, Any]] = []
    for path in source_options[:AMAP_MAX_ROUTE_OPTIONS]:
        option = _pick(
            path,
            (
                "distance",
                "duration",
                "strategy",
                "tolls",
                "toll_distance",
                "traffic_lights",
                "restriction",
            ),
        )
        if isinstance(path, dict):
            steps = _compact_steps(path.get("steps"))
            if steps:
                option["steps"] = steps
        if option:
            paths.append(option)
    compacted["paths"] = paths
    return compacted, len(source_options), len(paths)


def _write_raw_result(raw_dir: Path, tool_name: str, body: str) -> tuple[str, Path | None]:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    safe_tool = "".join(
        char if char.isalnum() or char in "-_" else "_"
        for char in tool_name
    )
    path = raw_dir / f"amap_{safe_tool}_{digest}.json"
    try:
        raw_dir.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(body, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return digest, path
    except OSError:
        # Reduction must still protect the model context even if diagnostic
        # persistence is unavailable.
        return digest, None


def reduce_mcp_body(
    *,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    body: str,
    raw_dir: Path,
) -> MCPBodyReduction:
    """Reduce large AMap route JSON while retaining travel-planning facts."""

    if (
        server_name.casefold() != "amap"
        or tool_name not in AMAP_ROUTE_TOOLS
        or len(body) < AMAP_REDUCTION_THRESHOLD
    ):
        return MCPBodyReduction(body=body, original_chars=len(body))

    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return MCPBodyReduction(body=body, original_chars=len(body))
    if not isinstance(payload, dict):
        return MCPBodyReduction(body=body, original_chars=len(body))

    container_name = "route" if isinstance(payload.get("route"), dict) else "data"
    route = payload.get(container_name)
    if not isinstance(route, dict):
        return MCPBodyReduction(body=body, original_chars=len(body))

    if tool_name == "maps_direction_transit_integrated":
        compacted_route, returned, included = _compact_transit_route(route)
    else:
        compacted_route, returned, included = _compact_path_route(route)

    raw_result_id, raw_result_path = _write_raw_result(raw_dir, tool_name, body)
    compacted_payload = {
        "_meta": {
            "provider": "amap",
            "tool": tool_name,
            "reduced": True,
            "original_chars": len(body),
            "route_options_returned": returned,
            "route_options_included": included,
            "raw_result_id": raw_result_id,
            "full_result_saved": raw_result_path is not None,
        },
        "request": arguments,
        container_name: compacted_route,
    }
    reduced_body = json.dumps(compacted_payload, ensure_ascii=False, indent=2)
    return MCPBodyReduction(
        body=reduced_body,
        reduced=True,
        original_chars=len(body),
        raw_result_id=raw_result_id,
        raw_result_path=raw_result_path,
    )
