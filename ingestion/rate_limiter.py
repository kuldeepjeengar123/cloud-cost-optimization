"""Sliding-window rate limiter for how many files the pipeline processes per minute."""
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._timestamps = deque()

    def acquire(self, block: bool = True) -> bool:
        """Reserve a processing slot. Returns True once a slot is available.
        If block=False, returns False immediately instead of waiting."""
        while True:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= 60:
                self._timestamps.popleft()

            if len(self._timestamps) < self.max_per_minute:
                self._timestamps.append(now)
                return True

            if not block:
                return False

            wait = 60 - (now - self._timestamps[0])
            time.sleep(max(wait, 0.05))
