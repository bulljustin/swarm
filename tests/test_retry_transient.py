"""Tests for integrations/retry.py — transient-failure retry helper."""

from __future__ import annotations

from unittest.mock import patch

import aiohttp
import pytest

from swarm.integrations.retry import retry_transient


def _response_error(status: int) -> aiohttp.ClientResponseError:
    from unittest.mock import MagicMock

    request_info = MagicMock()
    request_info.real_url = "https://example.test/api"
    return aiohttp.ClientResponseError(
        request_info=request_info,
        history=(),
        status=status,
        message="boom",
    )


@pytest.fixture(autouse=True)
def _no_sleep():
    async def instant(_delay):
        return None

    with patch("swarm.integrations.retry.asyncio.sleep", instant):
        yield


class TestRetryTransient:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        calls = []

        async def op():
            calls.append(1)
            return "ok"

        assert await retry_transient(op) == "ok"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_retries_transient_status_then_succeeds(self):
        calls = []

        async def op():
            calls.append(1)
            if len(calls) < 3:
                raise _response_error(503)
            return "ok"

        assert await retry_transient(op, attempts=3) == "ok"
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_non_transient_status_raises_immediately(self):
        calls = []

        async def op():
            calls.append(1)
            raise _response_error(404)

        with pytest.raises(aiohttp.ClientResponseError):
            await retry_transient(op, attempts=3)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_exhausted_attempts_raise_last_error(self):
        calls = []

        async def op():
            calls.append(1)
            raise _response_error(502)

        with pytest.raises(aiohttp.ClientResponseError):
            await retry_transient(op, attempts=3)
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_connection_errors_retried(self):
        calls = []

        async def op():
            calls.append(1)
            if len(calls) == 1:
                raise aiohttp.ClientConnectionError("reset")
            return 42

        assert await retry_transient(op, attempts=2) == 42
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_timeout_retried(self):
        calls = []

        async def op():
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError()
            return "late"

        assert await retry_transient(op, attempts=2) == "late"
