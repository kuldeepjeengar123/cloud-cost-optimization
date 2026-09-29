from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def invoke_with_retries(fn: Callable[[], T], max_attempts: int = 3, base_delay_seconds: float = 2.0) -> T:
    """Call fn() and retry on a transient provider failure (rate limits,
    empty/malformed responses, connection issues, timeouts), with a short
    linear backoff between attempts. Re-raises the last error if every
    attempt fails, so a genuine, persistent failure still surfaces instead
    of being hidden."""
    last_exc: Exception = RuntimeError("invoke_with_retries called with max_attempts < 1")
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - provider failures vary in exception type
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = base_delay_seconds * attempt
            print(
                f"  (transient error on attempt {attempt}/{max_attempts}: "
                f"{exc.__class__.__name__}: {exc}; retrying in {delay:.0f}s)"
            )
            time.sleep(delay)
    raise last_exc
