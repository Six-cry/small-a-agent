#!/usr/bin/env python3
"""Validate a structured travel plan without modifying any files."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any


REQUIRED_CONFIRMED_FIELDS = {
    "destination",
    "dates",
    "travelers",
    "arrival_departure",
    "budget",
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("plan root must be a JSON object")
    return data


def _parse_date(value: Any, field: str, errors: list[str]) -> date | None:
    if not isinstance(value, str):
        errors.append(f"{field} must be an ISO date string")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{field} must use YYYY-MM-DD")
        return None


def _non_negative_number(
    value: Any,
    field: str,
    errors: list[str],
) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        errors.append(f"{field} must be a non-negative finite number")
        return None
    return float(value)


def validate_plan(
    plan: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    trip_id = plan.get("id")
    if not isinstance(trip_id, str) or not trip_id.strip():
        errors.append("id must be a non-empty string")

    destination = plan.get("destination")
    if not isinstance(destination, dict):
        errors.append("destination must be an object")
    elif not isinstance(destination.get("city"), str) or not destination["city"].strip():
        errors.append("destination.city must be a non-empty string")

    start = _parse_date(plan.get("start_date"), "start_date", errors)
    end = _parse_date(plan.get("end_date"), "end_date", errors)
    expected_days: int | None = None
    expected_nights: int | None = None

    if start is not None and end is not None:
        if end < start:
            errors.append("end_date cannot be before start_date")
        else:
            expected_nights = (end - start).days
            expected_days = expected_nights + 1

    duration = plan.get("duration_days")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 1:
        errors.append("duration_days must be a positive integer")
    elif expected_days is not None and duration != expected_days:
        errors.append(
            f"duration_days must be {expected_days} for the supplied dates"
        )

    travelers = plan.get("travelers")
    if isinstance(travelers, bool) or not isinstance(travelers, int) or travelers < 1:
        errors.append("travelers must be a positive integer")

    accommodation = plan.get("accommodation")
    if not isinstance(accommodation, dict):
        errors.append("accommodation must be an object")
    else:
        nights = accommodation.get("nights")
        if isinstance(nights, bool) or not isinstance(nights, int) or nights < 0:
            errors.append("accommodation.nights must be a non-negative integer")
        elif expected_nights is not None and nights != expected_nights:
            errors.append(
                f"accommodation.nights must be {expected_nights} for the supplied dates"
            )

    budget = plan.get("budget")
    if not isinstance(budget, dict):
        errors.append("budget must be an object")
    else:
        total = _non_negative_number(budget.get("total"), "budget.total", errors)
        currency = budget.get("currency")
        if not isinstance(currency, str) or not currency.strip():
            errors.append("budget.currency must be a non-empty string")

        categories = budget.get("categories")
        category_total = 0.0
        if not isinstance(categories, dict) or not categories:
            errors.append("budget.categories must be a non-empty object")
        else:
            valid_categories = True
            for name, value in categories.items():
                parsed = _non_negative_number(
                    value,
                    f"budget.categories.{name}",
                    errors,
                )
                if parsed is None:
                    valid_categories = False
                else:
                    category_total += parsed
            if valid_categories and total is not None:
                if category_total > total + 0.01:
                    errors.append(
                        "budget category total cannot exceed budget.total "
                        f"({category_total:.2f} > {total:.2f})"
                    )
                elif total > 0 and category_total < total * 0.9:
                    warnings.append(
                        "budget categories allocate less than 90% of budget.total"
                    )

    itinerary = plan.get("itinerary")
    if not isinstance(itinerary, list) or not itinerary:
        errors.append("itinerary must be a non-empty list")
    else:
        if expected_days is not None and len(itinerary) != expected_days:
            errors.append(
                f"itinerary must contain {expected_days} day entries"
            )

        seen_days: set[int] = set()
        seen_dates: set[str] = set()
        for index, item in enumerate(itinerary):
            prefix = f"itinerary[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            day_number = item.get("day")
            if (
                isinstance(day_number, bool)
                or not isinstance(day_number, int)
                or day_number < 1
            ):
                errors.append(f"{prefix}.day must be a positive integer")
            elif day_number in seen_days:
                errors.append(f"{prefix}.day is duplicated")
            else:
                seen_days.add(day_number)

            item_date = _parse_date(item.get("date"), f"{prefix}.date", errors)
            if item_date is not None:
                date_text = item_date.isoformat()
                if date_text in seen_dates:
                    errors.append(f"{prefix}.date is duplicated")
                seen_dates.add(date_text)
                if start is not None and end is not None:
                    if not start <= item_date <= end:
                        errors.append(f"{prefix}.date is outside the trip dates")

            activities = item.get("activities")
            if not isinstance(activities, list) or not activities:
                errors.append(f"{prefix}.activities must be a non-empty list")

        if start is not None and expected_days is not None:
            expected_dates = {
                (start + timedelta(days=offset)).isoformat()
                for offset in range(expected_days)
            }
            missing_dates = sorted(expected_dates - seen_dates)
            if missing_dates:
                errors.append(
                    "itinerary is missing dates: " + ", ".join(missing_dates)
                )

    assumptions = plan.get("assumptions", [])
    if not isinstance(assumptions, list):
        errors.append("assumptions must be a list")
    elif mode == "save" and assumptions:
        errors.append("save mode requires critical assumptions to be resolved")

    confirmed_fields = plan.get("confirmed_fields", [])
    if not isinstance(confirmed_fields, list) or not all(
        isinstance(item, str) for item in confirmed_fields
    ):
        errors.append("confirmed_fields must be a list of strings")
    elif mode == "save":
        missing_confirmations = sorted(
            REQUIRED_CONFIRMED_FIELDS - set(confirmed_fields)
        )
        if missing_confirmations:
            errors.append(
                "save mode is missing confirmed fields: "
                + ", ".join(missing_confirmations)
            )

    arrival_departure = plan.get("arrival_departure")
    if mode == "save" and not isinstance(arrival_departure, dict):
        errors.append("save mode requires arrival_departure details")

    sources = plan.get("sources", [])
    if not isinstance(sources, list):
        errors.append("sources must be a list")
    elif mode in {"confirmed", "save"} and not sources:
        warnings.append(
            "no sources were recorded; current facts must be marked unverified"
        )

    return {
        "ok": not errors,
        "mode": mode,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "expected_days": expected_days,
            "expected_nights": expected_nights,
            "itinerary_days": len(itinerary) if isinstance(itinerary, list) else 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a structured travel plan."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--mode",
        choices=("draft", "confirmed", "save"),
        default="draft",
    )
    args = parser.parse_args()

    try:
        plan = _load_json(args.input.resolve())
        result = validate_plan(plan, args.mode)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "ok": False,
            "mode": args.mode,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "warnings": [],
            "summary": {},
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

