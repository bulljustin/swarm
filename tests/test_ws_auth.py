"""Tests for rate limiting logic from swarm.server.api.

Cleanup batch follow-up: imports were previously inside each test body,
which made the first test in the file pay the full cost of importing
``swarm.server.api`` (and its transitive aiohttp / asyncio setup) while
the 30s pytest-timeout was already counting. Under load that one-shot
import could push the test past the deadline even though the test body
itself runs in microseconds. Hoisting the imports to module level moves
the cost into collection where it's not timed.
"""

from __future__ import annotations

import time
from collections import deque

from swarm.server.api import _RATE_LIMIT_REQUESTS, _RATE_LIMIT_WINDOW


class TestRateLimitLogic:
    """Test the rate limit sliding window mechanism."""

    def test_hits_limit(self):
        timestamps: deque[float] = deque()
        now = time.time()

        for _ in range(_RATE_LIMIT_REQUESTS):
            timestamps.append(now)
        assert len(timestamps) >= _RATE_LIMIT_REQUESTS

    def test_below_limit(self):
        timestamps: deque[float] = deque()
        now = time.time()

        for _ in range(_RATE_LIMIT_REQUESTS - 1):
            timestamps.append(now)
        assert len(timestamps) < _RATE_LIMIT_REQUESTS

    def test_old_timestamps_pruned(self):
        timestamps: deque[float] = deque()
        old = time.time() - _RATE_LIMIT_WINDOW - 1
        for _ in range(100):
            timestamps.append(old)

        now = time.time()
        cutoff = now - _RATE_LIMIT_WINDOW
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        assert len(timestamps) == 0

    def test_mixed_old_and_new(self):
        timestamps: deque[float] = deque()
        old = time.time() - _RATE_LIMIT_WINDOW - 1
        now = time.time()
        for _ in range(5):
            timestamps.append(old)
        for _ in range(3):
            timestamps.append(now)

        cutoff = now - _RATE_LIMIT_WINDOW
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        assert len(timestamps) == 3
