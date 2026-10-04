from types import SimpleNamespace

import pytest

from aa_my_agent.recovery import (
    ModelRecoveryState,
    call_model_with_retry,
    classify_model_error,
    retry_after_seconds,
    retry_delay,
)


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(
            status_code=status_code,
            headers=headers or {},
        )


def recovery_call(call, state, **overrides):
    options = {
        "max_retries": 5,
        "base_delay_seconds": 0.5,
        "max_delay_seconds": 32.0,
        "max_consecutive_overloads": 3,
        "sleep": lambda _delay: None,
        "random_uniform": lambda _start, _end: 0.0,
    }
    options.update(overrides)
    return call_model_with_retry(call, state, **options)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ApiError("rate limited", 429), "rate_limit"),
        (ApiError("overloaded", 529), "overloaded"),
        (ApiError("service unavailable", 503), "transient"),
        (ApiError("context_length_exceeded", 400), "context_length"),
        (ApiError("invalid API key", 401), "fatal"),
    ],
)
def test_classifies_model_errors(error, expected):
    assert classify_model_error(error) == expected


def test_retry_delay_uses_retry_after_and_caps_it():
    error = ApiError("limited", 429, {"Retry-After": "90"})
    assert retry_after_seconds(error) == 90.0
    assert retry_delay(
        0,
        base_seconds=0.5,
        max_seconds=32.0,
        retry_after=retry_after_seconds(error),
    ) == 32.0


def test_overload_switches_to_fallback_for_next_real_attempt():
    state = ModelRecoveryState("primary", "fallback")
    models = []

    def call(model):
        models.append(model)
        if len(models) <= 3:
            raise ApiError("overloaded", 529)
        return "ok"

    assert recovery_call(call, state) == "ok"
    assert models == ["primary", "primary", "primary", "fallback"]
    assert state.current_model == "fallback"
    assert state.fallback_switches == 1


def test_rate_limit_retries_with_exponential_delays():
    state = ModelRecoveryState("primary")
    sleeps = []
    calls = 0

    def call(_model):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ApiError("rate limited", 429)
        return "ok"

    assert recovery_call(
        call,
        state,
        sleep=sleeps.append,
    ) == "ok"
    assert sleeps == [0.5, 1.0]
    assert state.transient_retries == 2


def test_fatal_and_context_errors_are_not_retried():
    for error in (
        ApiError("invalid API key", 401),
        ApiError("context_length_exceeded", 400),
    ):
        state = ModelRecoveryState("primary")
        calls = 0

        def call(_model):
            nonlocal calls
            calls += 1
            raise error

        with pytest.raises(ApiError):
            recovery_call(call, state)
        assert calls == 1
