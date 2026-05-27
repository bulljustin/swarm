"""Direct tests for :func:`apply_llms` and :func:`apply_provider_overrides`.

The two appliers landed in ``swarm.server.config_appliers.llms`` during
the ConfigManager refactor (2026.5.26.6) but were never directly
tested — pre-refactor coverage came through ``test_config_manager.py``
exercising ``ConfigManager._apply_llms`` indirectly via
``apply_update``, and that path didn't survive the move.  Coverage
gap closed in the 2026-05-27 test-gap fill-in.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.config import HiveConfig
from swarm.server.config_appliers._base import ApplierDeps
from swarm.server.config_appliers.llms import apply_llms, apply_provider_overrides


def _deps() -> ApplierDeps:
    """Build an ApplierDeps with mocked side-effect handles."""
    return ApplierDeps(
        invalidate_provider_cache=MagicMock(),
        get_worker_svc=MagicMock(return_value=None),
    )


# ---------------------------------------------------------------------------
# apply_llms
# ---------------------------------------------------------------------------


class TestApplyLlmsValidation:
    """Body-shape and per-entry validation errors."""

    def test_non_list_body_raises(self) -> None:
        cfg = HiveConfig()
        with pytest.raises(ValueError, match="llms must be a list"):
            apply_llms(cfg, {"name": "foo"}, deps=_deps())

    def test_non_dict_entry_raises(self) -> None:
        cfg = HiveConfig()
        with pytest.raises(ValueError, match=r"llms\[0\] must be an object"):
            apply_llms(cfg, ["not-a-dict"], deps=_deps())

    def test_missing_name_raises(self) -> None:
        cfg = HiveConfig()
        with pytest.raises(ValueError, match="name is required"):
            apply_llms(cfg, [{"command": ["foo"]}], deps=_deps())

    def test_empty_name_raises(self) -> None:
        cfg = HiveConfig()
        with pytest.raises(ValueError, match="name is required"):
            apply_llms(cfg, [{"name": "   ", "command": ["foo"]}], deps=_deps())

    def test_builtin_collision_raises(self) -> None:
        """``claude``/``gemini``/``codex``/``opencode`` are reserved."""
        cfg = HiveConfig()
        with pytest.raises(ValueError, match="collides with built-in provider"):
            apply_llms(cfg, [{"name": "claude", "command": ["x"]}], deps=_deps())

    def test_duplicate_name_raises(self) -> None:
        cfg = HiveConfig()
        body = [
            {"name": "mycli", "command": ["a"]},
            {"name": "mycli", "command": ["b"]},
        ]
        with pytest.raises(ValueError, match="duplicate name 'mycli'"):
            apply_llms(cfg, body, deps=_deps())

    def test_missing_command_raises(self) -> None:
        cfg = HiveConfig()
        with pytest.raises(ValueError, match="command is required"):
            apply_llms(cfg, [{"name": "mycli"}], deps=_deps())

    def test_string_command_splits_into_list(self) -> None:
        """``command: "uv run mycli"`` should split into ``["uv", "run", "mycli"]``."""
        cfg = HiveConfig()
        body = [{"name": "mycli", "command": "uv run mycli"}]
        apply_llms(cfg, body, deps=_deps())
        assert cfg.custom_llms[0].command == ["uv", "run", "mycli"]


class TestApplyLlmsHappyPath:
    """Successful apply paths populate cfg + fire the cache-invalidate hook."""

    def test_single_entry_lands_on_cfg(self) -> None:
        cfg = HiveConfig()
        body = [{"name": "mycli", "command": ["uv", "run", "mycli"]}]
        outcome = apply_llms(cfg, body, deps=_deps())
        assert outcome.consumed == []  # apply_llms doesn't track field-level consumed
        assert outcome.unknown == []
        assert len(cfg.custom_llms) == 1
        llm = cfg.custom_llms[0]
        assert llm.name == "mycli"
        assert llm.command == ["uv", "run", "mycli"]
        assert llm.display_name == ""

    def test_display_name_preserved(self) -> None:
        cfg = HiveConfig()
        body = [
            {"name": "mycli", "command": ["x"], "display_name": "  My CLI  "},
        ]
        apply_llms(cfg, body, deps=_deps())
        # display_name is strip()'d
        assert cfg.custom_llms[0].display_name == "My CLI"

    def test_invalidate_provider_cache_fires(self) -> None:
        cfg = HiveConfig()
        deps = _deps()
        apply_llms(cfg, [{"name": "mycli", "command": ["x"]}], deps=deps)
        deps.invalidate_provider_cache.assert_called_once()

    def test_empty_list_clears_custom_llms(self) -> None:
        """Sending ``llms: []`` is a destructive overwrite — by design.

        Distinct from the workflows-empty guard: ``llms`` is a list of
        full provider declarations, and the dashboard sends the
        complete authoritative list on every save.
        """
        from swarm.config.models import CustomLLMConfig

        cfg = HiveConfig()
        cfg.custom_llms = [CustomLLMConfig(name="stale", command=["old"])]
        apply_llms(cfg, [], deps=_deps())
        assert cfg.custom_llms == []

    def test_tuning_regex_validated(self) -> None:
        """Each tuning regex field is compiled at apply time."""
        cfg = HiveConfig()
        body = [
            {
                "name": "mycli",
                "command": ["x"],
                "idle_pattern": "[invalid",
            }
        ]
        with pytest.raises(ValueError, match="idle_pattern: invalid regex"):
            apply_llms(cfg, body, deps=_deps())


# ---------------------------------------------------------------------------
# apply_provider_overrides
# ---------------------------------------------------------------------------


class TestApplyProviderOverridesValidation:
    """Body-shape and per-entry validation errors."""

    def test_non_dict_body_raises(self) -> None:
        cfg = HiveConfig()
        with pytest.raises(ValueError, match="provider_overrides must be an object"):
            apply_provider_overrides(cfg, ["claude"], deps=_deps())

    def test_unknown_provider_raises(self) -> None:
        cfg = HiveConfig()
        with pytest.raises(ValueError, match="unknown provider 'openai'"):
            apply_provider_overrides(cfg, {"openai": {}}, deps=_deps())

    def test_non_dict_entry_raises(self) -> None:
        cfg = HiveConfig()
        with pytest.raises(ValueError, match=r"provider_overrides\.claude must be an object"):
            apply_provider_overrides(cfg, {"claude": "not-a-dict"}, deps=_deps())


class TestApplyProviderOverridesHappyPath:
    """Successful overrides reach cfg + fire the cache-invalidate hook."""

    def test_empty_dict_resets_overrides(self) -> None:
        from swarm.config import ProviderTuning

        cfg = HiveConfig()
        cfg.provider_overrides = {"claude": ProviderTuning(idle_pattern="old")}
        apply_provider_overrides(cfg, {}, deps=_deps())
        assert cfg.provider_overrides == {}

    def test_empty_per_provider_drops_via_has_tuning(self) -> None:
        """``{"claude": {}}`` produces a tuning with ``has_tuning() == False``,
        which is filtered out — protects against the dashboard sending an
        empty override row from accidentally pinning empty tuning."""
        cfg = HiveConfig()
        apply_provider_overrides(cfg, {"claude": {}}, deps=_deps())
        assert cfg.provider_overrides == {}

    def test_real_tuning_lands_on_cfg(self) -> None:
        cfg = HiveConfig()
        body = {
            "claude": {
                "idle_pattern": r"\$ $",
                "busy_pattern": r"esc to interrupt",
            }
        }
        apply_provider_overrides(cfg, body, deps=_deps())
        assert "claude" in cfg.provider_overrides
        tuning = cfg.provider_overrides["claude"]
        assert tuning.idle_pattern == r"\$ $"
        assert tuning.busy_pattern == r"esc to interrupt"

    def test_invalidate_provider_cache_fires(self) -> None:
        cfg = HiveConfig()
        deps = _deps()
        apply_provider_overrides(cfg, {"claude": {"idle_pattern": r"\$ $"}}, deps=deps)
        deps.invalidate_provider_cache.assert_called_once()

    def test_bad_regex_per_provider_raises(self) -> None:
        cfg = HiveConfig()
        body = {"claude": {"idle_pattern": "[unterminated"}}
        with pytest.raises(ValueError, match="idle_pattern: invalid regex"):
            apply_provider_overrides(cfg, body, deps=_deps())
