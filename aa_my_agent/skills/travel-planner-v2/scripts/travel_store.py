#!/usr/bin/env python3
"""Safely store explicitly confirmed travel plans in the aa_my_agent workspace."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from validate_trip import validate_plan


APP_DIR = Path(__file__).resolve().parents[3]
DEFAULT_DB = APP_DIR / "storage" / "travel_planner_v2" / "trips.json"


def _empty_database() -> dict[str, list[dict[str, Any]]]:
    return {"trips": []}


def _read_database(path: Path) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        return _empty_database()

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict) or not isinstance(data.get("trips"), list):
        raise ValueError("travel database must contain a trips list")
    return data


def _read_plan(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        plan = json.load(handle)
    if not isinstance(plan, dict):
        raise ValueError("plan root must be a JSON object")
    return plan


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix="trips-",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _find_trip(
    database: dict[str, list[dict[str, Any]]],
    trip_id: str,
) -> dict[str, Any] | None:
    for trip in database["trips"]:
        if isinstance(trip, dict) and trip.get("id") == trip_id:
            return trip
    return None


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Store explicitly confirmed travel plans."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list")

    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("--id", required=True)

    save_parser = subparsers.add_parser("save")
    save_parser.add_argument("--input", required=True, type=Path)
    save_parser.add_argument(
        "--confirmed-by-user",
        action="store_true",
        help="Required acknowledgement that the user explicitly requested saving.",
    )

    args = parser.parse_args()
    database_path = args.db.resolve()

    try:
        database = _read_database(database_path)

        if args.command == "list":
            _print_json(
                [
                    {
                        "id": trip.get("id"),
                        "destination": trip.get("destination"),
                        "start_date": trip.get("start_date"),
                        "end_date": trip.get("end_date"),
                    }
                    for trip in database["trips"]
                    if isinstance(trip, dict)
                ]
            )
            return 0

        if args.command == "get":
            trip = _find_trip(database, args.id)
            if trip is None:
                _print_json({"ok": False, "error": "trip not found"})
                return 1
            _print_json({"ok": True, "trip": trip})
            return 0

        if not args.confirmed_by_user:
            _print_json(
                {
                    "ok": False,
                    "error": (
                        "save refused: --confirmed-by-user is required "
                        "after an explicit user request"
                    ),
                }
            )
            return 1

        plan = _read_plan(args.input.resolve())
        validation = validate_plan(plan, mode="save")
        if not validation["ok"]:
            _print_json(
                {
                    "ok": False,
                    "error": "plan validation failed",
                    "validation": validation,
                }
            )
            return 1

        trip_id = str(plan["id"])
        if _find_trip(database, trip_id) is not None:
            _print_json(
                {
                    "ok": False,
                    "error": f"trip id already exists: {trip_id}",
                }
            )
            return 1

        database["trips"].append(plan)
        _atomic_write(database_path, database)

        saved_database = _read_database(database_path)
        saved_trip = _find_trip(saved_database, trip_id)
        if saved_trip is None:
            raise RuntimeError("saved trip could not be read back")

        _print_json(
            {
                "ok": True,
                "db": str(database_path),
                "trip_id": trip_id,
                "read_back": True,
            }
        )
        return 0

    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        _print_json(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
