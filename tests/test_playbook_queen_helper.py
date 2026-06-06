"""Tests for the shared playbook Queen-invoke helper (#playbooks-audit B)."""

from __future__ import annotations

import asyncio

import pytest

from swarm.playbooks._queen import run_queen_json


class _Queen:
    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = 0

    async def ask(self, prompt, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.mark.asyncio
async def test_returns_verdict_and_calls_stateless():
    q = _Queen(result={"synthesize": True})
    out = await run_queen_json(q, "prompt", context="unit")
    assert out == {"synthesize": True}
    assert q.calls == 1
    assert q.last_kwargs.get("stateless") is True  # always stateless


@pytest.mark.asyncio
async def test_swallows_exception_returns_none():
    q = _Queen(exc=RuntimeError("boom"))
    assert await run_queen_json(q, "p", context="unit") is None


@pytest.mark.asyncio
async def test_reraises_cancelled_error():
    q = _Queen(exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run_queen_json(q, "p", context="unit")
