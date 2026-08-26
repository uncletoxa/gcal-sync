from __future__ import annotations

import random
import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


def compute_delay(attempt: int, base_delay: float = 1.0, max_delay: float = 30.0) -> float:
    """Exponential backoff with jitter. `attempt` is 1-indexed."""
    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
    return delay + random.uniform(0, delay * 0.1)


def retry_call(
    fn: Callable[[], T],
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    is_retryable: Callable[[Exception], bool] = lambda exc: True,
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
) -> T:
    """Call `fn`, retrying with exponential backoff while `is_retryable(exc)` is True."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            if attempt >= max_attempts or not is_retryable(exc):
                raise
            delay = compute_delay(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            time.sleep(delay)
