"""Tests for PlaybookSynthesizer (playbook-synthesis-loop Phase 1)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from swarm.config.models import PlaybookConfig
from swarm.db.core import SwarmDB
from swarm.db.playbook_store import PlaybookStore
from swarm.playbooks.synthesizer import PlaybookSynthesizer


@dataclass
class _Type:
    value: str


@dataclass
class _Task:
    id: str = "t-1"
    title: str = "Add retry to webhook sender"
    description: str = "Sender dropped events on 5xx"
    task_type: _Type = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.task_type is None:
            self.task_type = _Type("feature")


class _Queen:
    """Configurable fake headless Queen; counts ask() calls."""

    def __init__(self, verdict=None, raises=False):
        self.verdict = verdict if verdict is not None else {"synthesize": False}
        self.raises = raises
        self.calls = 0

    async def ask(self, prompt, **kwargs):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        return self.verdict


_GOOD = {
    "synthesize": True,
    "name": "Webhook Retry With Backoff",
    "title": "Webhook retry with backoff",
    "scope": "global",
    "trigger": "when an outbound sender drops events on 5xx",
    "body": "1. wrap send in retry\n2. exp backoff\n3. dead-letter after N",
    "confidence": 0.82,
}

_RESOLUTION = (
    "Added exponential-backoff retry around the webhook sender; 5xx now "
    "retries 4x then dead-letters. Regression test added; /check green."
)


def _synth(tmp_path, *, queen, cfg=None, drone_log=None, now=None):
    db = SwarmDB(tmp_path / "swarm.db")
    store = PlaybookStore(db)
    kwargs = {}
    if now is not None:
        kwargs["now"] = now
    return (
        PlaybookSynthesizer(
            queen=queen,
            store=store,
            config=cfg or PlaybookConfig(),
            drone_log=drone_log,
            **kwargs,
        ),
        store,
    )


@pytest.mark.asyncio
async def test_synthesize_creates_candidate(tmp_path):
    q = _Queen(_GOOD)
    s, store = _synth(tmp_path, queen=q)
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is not None
    assert pb.name == "webhook-retry-with-backoff"
    assert pb.status.value == "candidate"
    assert pb.provenance_task_ids == ["t-1"]
    assert pb.source_worker == "swarm"
    assert store.get("webhook-retry-with-backoff") is not None
    assert q.calls == 1


@pytest.mark.asyncio
async def test_decline_creates_nothing(tmp_path):
    q = _Queen({"synthesize": False})
    s, store = _synth(tmp_path, queen=q)
    assert await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION) is None
    assert store.list() == []
    assert q.calls == 1


@pytest.mark.asyncio
async def test_memoized_one_call_per_worker_task(tmp_path):
    q = _Queen(_GOOD)
    s, _ = _synth(tmp_path, queen=q)
    t = _Task()
    first = await s.synthesize(t, worker="swarm", resolution=_RESOLUTION)
    second = await s.synthesize(t, worker="swarm", resolution=_RESOLUTION)
    assert first is not None
    assert second is None
    assert q.calls == 1  # second fire is memoized — no extra Queen call


@pytest.mark.asyncio
async def test_ineligible_task_type_skipped(tmp_path):
    q = _Queen(_GOOD)
    s, _ = _synth(tmp_path, queen=q)
    t = _Task(task_type=_Type("verify"))
    assert await s.synthesize(t, worker="swarm", resolution=_RESOLUTION) is None
    assert q.calls == 0  # gated before any Queen call


@pytest.mark.asyncio
async def test_short_resolution_skipped(tmp_path):
    q = _Queen(_GOOD)
    s, _ = _synth(tmp_path, queen=q)
    assert await s.synthesize(_Task(), worker="swarm", resolution="done") is None
    assert q.calls == 0


@pytest.mark.asyncio
async def test_rate_cap_enforced(tmp_path):
    q = _Queen(_GOOD)
    s, _ = _synth(tmp_path, queen=q, cfg=PlaybookConfig(max_synth_per_hour=1))
    a = await s.synthesize(_Task(id="t-1"), worker="swarm", resolution=_RESOLUTION)
    b = await s.synthesize(_Task(id="t-2"), worker="swarm", resolution=_RESOLUTION)
    assert a is not None
    assert b is None
    assert q.calls == 1  # second distinct task blocked by per-hour cap


@pytest.mark.asyncio
async def test_queen_error_is_safe(tmp_path):
    s, store = _synth(tmp_path, queen=_Queen(raises=True))
    assert await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION) is None
    assert store.list() == []


@pytest.mark.asyncio
async def test_queen_error_dict_is_safe(tmp_path):
    s, store = _synth(tmp_path, queen=_Queen({"error": "Rate limited"}))
    assert await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION) is None
    assert store.list() == []


@pytest.mark.asyncio
async def test_invalid_scope_defaults_global(tmp_path):
    bad = dict(_GOOD, scope="nonsense::x")
    s, _ = _synth(tmp_path, queen=_Queen(bad))
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is not None and pb.scope == "global"


@pytest.mark.asyncio
async def test_synthesize_caps_oversized_body(tmp_path):
    """#playbooks-audit A: a runaway Queen body is truncated to MAX_BODY_LEN so
    it can't bloat the DB / SKILL.md."""
    from swarm.playbooks.models import MAX_BODY_LEN

    verdict = dict(_GOOD)
    verdict["body"] = "x" * (MAX_BODY_LEN + 5000)
    s, store = _synth(tmp_path, queen=_Queen(verdict))
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is not None
    assert len(pb.body) == MAX_BODY_LEN


# ---------------------------------------------------------------------------
# #894: low-confidence + recent-equivalent synthesis gates
# ---------------------------------------------------------------------------


class _RecordingLog:
    def __init__(self):
        self.entries = []

    def add(self, action, worker, detail, *, category=None):
        self.entries.append((action, worker, detail))


@pytest.mark.asyncio
async def test_low_confidence_playbook_is_gated(tmp_path):
    """#894 cond 1: a conf=0.00 (sub-floor) playbook is NOT auto-synthesized —
    it's dropped + logged PLAYBOOK_GATED for approval, not persisted."""
    from swarm.drones.log import SystemAction

    verdict = dict(_GOOD)
    verdict["confidence"] = 0.0  # the degenerate fleet-wide case
    verdict["scope"] = "global"
    log = _RecordingLog()
    s, store = _synth(tmp_path, queen=_Queen(verdict), drone_log=log)
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is None  # not synthesized
    assert store.get("webhook-retry-with-backoff") is None  # not persisted
    assert any(a is SystemAction.PLAYBOOK_GATED for a, _, _ in log.entries)


@pytest.mark.asyncio
async def test_confidence_above_floor_synthesizes(tmp_path):
    """Control: a confident playbook (0.82 > floor 0.3) still synthesizes."""
    s, store = _synth(tmp_path, queen=_Queen(_GOOD))  # _GOOD conf=0.82
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is not None
    assert store.get("webhook-retry-with-backoff") is not None


@pytest.mark.asyncio
async def test_confidence_floor_zero_disables_gate(tmp_path):
    """min_synthesis_confidence=0 disables the floor (legacy behaviour)."""
    cfg = PlaybookConfig(min_synthesis_confidence=0.0)
    verdict = dict(_GOOD)
    verdict["confidence"] = 0.0
    s, store = _synth(tmp_path, queen=_Queen(verdict), cfg=cfg)
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is not None  # floor disabled → persists even at 0.0


@pytest.mark.asyncio
async def test_recent_equivalent_is_skipped(tmp_path):
    """#894 cond 2: a same-name playbook created within the window means an
    equivalent was just synthesized — re-synthesis is gated, not re-spawned."""
    from swarm.drones.log import SystemAction
    from swarm.playbooks.models import Playbook

    now = 1_000_000.0
    log = _RecordingLog()
    s, store = _synth(tmp_path, queen=_Queen(_GOOD), drone_log=log, now=lambda: now)
    # An equivalent program was synthesized 10 minutes ago.
    store.create(
        Playbook(name="webhook-retry-with-backoff", body="prior body", created_at=now - 600)
    )
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is None
    assert any(a is SystemAction.PLAYBOOK_GATED for a, _, _ in log.entries)


@pytest.mark.asyncio
async def test_old_equivalent_does_not_block(tmp_path):
    """An equivalent created OUTSIDE the window doesn't block re-synthesis."""
    from swarm.playbooks.models import Playbook

    now = 1_000_000.0
    cfg = PlaybookConfig(resynthesis_window_seconds=3600.0)  # 1h window
    s, store = _synth(tmp_path, queen=_Queen(_GOOD), cfg=cfg, now=lambda: now)
    # Same name + same body as _GOOD → store.create folds it (content_hash
    # match); created 2h ago so the gate's 1h window doesn't fire.
    store.create(
        Playbook(name="webhook-retry-with-backoff", body=_GOOD["body"], created_at=now - 7200)
    )
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is not None  # 2h-old equivalent is outside the 1h window → folds, not gated
