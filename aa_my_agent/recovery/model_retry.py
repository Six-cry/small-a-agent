"""Model-call recovery primitives shared by Agent loops.

Only model requests are retried here. Tool execution deliberately stays
outside this module so a transient API failure cannot repeat side effects.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar


T = TypeVar("T")


@dataclass
class ModelRecoveryState:
    primary_model: str
    fallback_model: str | None = None
    current_model: str = ""
    consecutive_overloads: int = 0
    transient_retries: int = 0
    fallback_switches: int = 0
    context_compactions: int = 0

    def __post_init__(self) -> None:
        if not self.current_model:
            self.current_model = self.primary_model


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def classify_model_error(exc: Exception) -> str:
    """Classify an SDK/API exception into one recovery category."""
    status = _status_code(exc)
    name = type(exc).__name__.casefold()
    message = str(exc).casefold()

    context_markers = (
        "prompt_too_long",
        "prompt is too long",
        "prompt too long",
        "too many tokens",
        "context_length_exceeded",
        "maximum context length",
        "max_context_window",
        "input is too long",
        "context window",
    )
    if status in {400, 413} and any(
        marker in message for marker in context_markers
    ):
        return "context_length"
    if any(marker in message for marker in context_markers):
        return "context_length"

    if status == 429 or "ratelimit" in name or "rate limit" in message:
        return "rate_limit"
    if status == 529 or "overloaded" in name or "overloaded" in message:
        return "overloaded"
    if status in {408, 409, 425, 500, 502, 503, 504}:
        return "transient"
    if any(
        marker in name
        for marker in ("timeout", "connection", "internalserver")
    ):
        return "transient"
    if any(
        marker in message
        for marker in (
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "temporarily unavailable",
            "service unavailable",
        )
    ):
        return "transient"
    return "fatal"


def _response_headers(exc: Exception):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        return headers
    return getattr(exc, "headers", None)


def retry_after_seconds(exc: Exception) -> float | None:
    """Read Retry-After seconds or HTTP-date from a model API error."""
    headers = _response_headers(exc)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(str(raw))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (retry_at - datetime.now(timezone.utc)).total_seconds(),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(
    retry_index: int,
    *,
    base_seconds: float,
    max_seconds: float,
    retry_after: float | None = None,
    random_uniform: Callable[[float, float], float] = random.uniform,
) -> float:
    """Return capped exponential backoff with up to 25% jitter."""
    if retry_after is not None:
        return min(max_seconds, max(0.0, retry_after))
    base = min(max_seconds, base_seconds * (2 ** retry_index))
    return min(max_seconds, base + random_uniform(0.0, base * 0.25))


def call_model_with_retry(
    call: Callable[[str], T],
    state: ModelRecoveryState,
    *,
    max_retries: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    max_consecutive_overloads: int,
    sleep: Callable[[float], None] = time.sleep,
    random_uniform: Callable[[float, float], float] = random.uniform,
    on_event: Callable[..., None] | None = None,
) -> T:
    """Call the current model and retry only recognized transient errors."""
    retry_index = 0
    while True:
        try:
            result = call(state.current_model)
            state.consecutive_overloads = 0
            return result
        except Exception as exc:
            kind = classify_model_error(exc)
            if kind not in {"rate_limit", "overloaded", "transient"}:
                raise

            if retry_index >= max_retries:
                if on_event:
                    on_event(
                        "exhausted",
                        kind=kind,
                        retry=retry_index,
                        model=state.current_model,
                        error=type(exc).__name__,
                    )
                raise

            switched = False
            if kind == "overloaded":
                state.consecutive_overloads += 1
                if (
                    state.consecutive_overloads
                    >= max_consecutive_overloads
                    and state.fallback_model
                    and state.current_model != state.fallback_model
                ):
                    state.current_model = state.fallback_model
                    state.consecutive_overloads = 0
                    state.fallback_switches += 1
                    switched = True
            else:
                state.consecutive_overloads = 0

            delay = retry_delay(
                retry_index,
                base_seconds=base_delay_seconds,
                max_seconds=max_delay_seconds,
                retry_after=retry_after_seconds(exc),
                random_uniform=random_uniform,
            )
            retry_index += 1
            state.transient_retries += 1
            if on_event:
                on_event(
                    "retry",
                    kind=kind,
                    retry=retry_index,
                    max_retries=max_retries,
                    delay=f"{delay:.2f}s",
                    model=state.current_model,
                    fallback_switched=switched,
                    error=type(exc).__name__,
                )
            sleep(delay)
