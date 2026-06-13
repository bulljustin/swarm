"""Transient-failure retry for external API calls (Jira, Graph).

One-shot side-effect calls (transition a Jira issue, post a completion
comment) had no second chance: a single 503 lost the export silently and
the swarm's state drifted from the external system's. Read paths don't
need this — they run inside periodic sync loops that retry by design.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import aiohttp

from swarm.logging import get_logger

_log = get_logger("integrations.retry")

# 429 + 5xx are worth a retry; 4xx (auth, validation, not-found) never heal
# on their own.
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


def is_transient_status(status: int) -> bool:
    return status in TRANSIENT_STATUSES


async def retry_transient[T](
    op: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    what: str = "request",
) -> T:
    """Run ``op``, retrying transient HTTP / network failures with backoff.

    Retries on ``aiohttp.ClientResponseError`` with a transient status,
    connection-level errors, and timeouts. Non-transient response errors
    and the final attempt's exception propagate to the caller.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except aiohttp.ClientResponseError as e:
            if not is_transient_status(e.status):
                raise
            last_exc = e
        except (aiohttp.ClientConnectionError, TimeoutError) as e:
            last_exc = e
        if attempt == attempts:
            break
        delay = base_delay * 2 ** (attempt - 1)
        _log.warning(
            "%s failed (attempt %d/%d): %s — retrying in %.1fs",
            what,
            attempt,
            attempts,
            last_exc,
            delay,
        )
        await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
