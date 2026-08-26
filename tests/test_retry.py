import pytest

from gcal_sync.retry import retry_call


def test_retry_recovers_after_transient_failures(monkeypatch):
    monkeypatch.setattr("gcal_sync.retry.time.sleep", lambda _: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    result = retry_call(flaky, max_attempts=5, base_delay=0.01, is_retryable=lambda exc: isinstance(exc, ValueError))

    assert result == "ok"
    assert calls["n"] == 3


def test_retry_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("gcal_sync.retry.time.sleep", lambda _: None)
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise ValueError("still broken")

    with pytest.raises(ValueError):
        retry_call(always_fails, max_attempts=3, base_delay=0.01, is_retryable=lambda exc: True)

    assert calls["n"] == 3


def test_retry_does_not_retry_non_retryable_errors(monkeypatch):
    monkeypatch.setattr("gcal_sync.retry.time.sleep", lambda _: None)
    calls = {"n": 0}

    def fails_permanently():
        calls["n"] += 1
        raise KeyError("permanent")

    with pytest.raises(KeyError):
        retry_call(fails_permanently, max_attempts=5, base_delay=0.01, is_retryable=lambda exc: isinstance(exc, ValueError))

    assert calls["n"] == 1
