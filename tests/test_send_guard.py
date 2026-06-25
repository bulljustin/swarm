"""Tests for the inter-worker send-path guards (task #873).

Covers :func:`resolve_recipient` (recipient-namespace validation) and
:class:`FanoutGuard` (per-sender identical-message fan-out cap).
"""

from __future__ import annotations

from swarm.messages.send_guard import FanoutGuard, resolve_recipient


class TestResolveRecipient:
    def test_exact_match_returns_name(self) -> None:
        assert resolve_recipient({"hub", "platform"}, "hub") == "hub"

    def test_case_insensitive_match_returns_canonical(self) -> None:
        # A worker addressing "Hub" should resolve to the roster's "hub" so
        # the persisted row lines up with get_unread("hub").
        assert resolve_recipient({"hub", "platform"}, "Hub") == "hub"
        assert resolve_recipient({"hub", "platform"}, "PLATFORM") == "platform"

    def test_unknown_name_returns_none(self) -> None:
        assert resolve_recipient({"hub", "platform"}, "aria") is None
        assert resolve_recipient({"hub", "platform"}, "sillytavern") is None

    def test_empty_roster_returns_none(self) -> None:
        assert resolve_recipient(set(), "hub") is None


class TestFanoutGuard:
    def test_allows_up_to_cap_distinct_recipients(self) -> None:
        g = FanoutGuard(max_recipients=3, window_seconds=60.0)
        assert g.check("platform", "hub", "same body", now=0.0) is True
        assert g.check("platform", "nexus", "same body", now=1.0) is True
        assert g.check("platform", "admin", "same body", now=2.0) is True

    def test_blocks_beyond_cap_for_identical_content(self) -> None:
        g = FanoutGuard(max_recipients=3, window_seconds=60.0)
        for i, r in enumerate(["hub", "nexus", "admin"]):
            assert g.check("platform", r, "same body", now=float(i)) is True
        # 4th distinct recipient, identical content, within window → blocked.
        assert g.check("platform", "root", "same body", now=3.0) is False
        assert g.check("platform", "budgetbug", "same body", now=4.0) is False

    def test_resend_to_counted_recipient_is_allowed(self) -> None:
        """A retry to an already-counted recipient doesn't widen the blast
        radius, so it stays allowed even at the cap."""
        g = FanoutGuard(max_recipients=2, window_seconds=60.0)
        assert g.check("platform", "hub", "body", now=0.0) is True
        assert g.check("platform", "nexus", "body", now=1.0) is True
        # at cap, but hub is already counted → allowed
        assert g.check("platform", "hub", "body", now=2.0) is True
        # a NEW distinct recipient at cap → blocked
        assert g.check("platform", "admin", "body", now=3.0) is False

    def test_different_content_has_separate_budget(self) -> None:
        """The cap is per identical message — distinct findings to distinct
        workers are legitimate coordination, not a fan-out."""
        g = FanoutGuard(max_recipients=2, window_seconds=60.0)
        assert g.check("platform", "hub", "finding A", now=0.0) is True
        assert g.check("platform", "nexus", "finding A", now=1.0) is True
        # Different body → fresh budget, not blocked.
        assert g.check("platform", "admin", "finding B", now=2.0) is True
        assert g.check("platform", "root", "finding B", now=3.0) is True

    def test_whitespace_variants_share_budget(self) -> None:
        """Trivially reformatted copies of the same memo count together."""
        g = FanoutGuard(max_recipients=2, window_seconds=60.0)
        assert g.check("platform", "hub", "the   SAME body", now=0.0) is True
        assert g.check("platform", "nexus", "the same body", now=1.0) is True
        assert g.check("platform", "admin", "The Same Body", now=2.0) is False

    def test_window_expiry_resets_budget(self) -> None:
        g = FanoutGuard(max_recipients=2, window_seconds=60.0)
        assert g.check("platform", "hub", "body", now=0.0) is True
        assert g.check("platform", "nexus", "body", now=1.0) is True
        assert g.check("platform", "admin", "body", now=2.0) is False
        # Past the window the earlier recipients expire → budget frees up.
        assert g.check("platform", "admin", "body", now=120.0) is True

    def test_separate_senders_have_separate_budgets(self) -> None:
        g = FanoutGuard(max_recipients=2, window_seconds=60.0)
        assert g.check("platform", "hub", "body", now=0.0) is True
        assert g.check("platform", "nexus", "body", now=1.0) is True
        assert g.check("platform", "admin", "body", now=2.0) is False
        # A different sender is unaffected by platform's budget.
        assert g.check("admin", "hub", "body", now=3.0) is True
        assert g.check("admin", "nexus", "body", now=4.0) is True

    def test_disabled_when_cap_non_positive(self) -> None:
        g = FanoutGuard(max_recipients=0, window_seconds=60.0)
        assert g.enabled is False
        for i in range(50):
            assert g.check("platform", f"w{i}", "body", now=float(i)) is True

    def test_disabled_when_window_non_positive(self) -> None:
        g = FanoutGuard(max_recipients=5, window_seconds=0.0)
        assert g.enabled is False
        for i in range(50):
            assert g.check("platform", f"w{i}", "body", now=float(i)) is True

    def test_twenty_recipient_burst_is_capped(self) -> None:
        """Acceptance #873: the exact symptom — one sender hand-enumerating
        the roster with an identical memo. Only the first ``max_recipients``
        distinct recipients get through; the rest are blocked."""
        g = FanoutGuard(max_recipients=5, window_seconds=60.0)
        allowed = sum(
            1 for i in range(24) if g.check("platform", f"worker-{i}", "uuid v14 memo", now=0.0)
        )
        assert allowed == 5
