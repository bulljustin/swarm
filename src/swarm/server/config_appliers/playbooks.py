"""``playbooks`` section applier — synth loop tuning."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.server.config_manager import FieldOutcome, _apply_dataclass_dict

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


# Playbook fields that must lie in [0.0, 1.0] (winrate / similarity
# thresholds). REST endpoint is publicly addressable so the dashboard
# sliders alone don't protect against a direct bad POST.
_UNIT_INTERVAL_FLOATS: frozenset[str] = frozenset(
    {"auto_promote_winrate", "prune_max_winrate", "dedupe_similarity_threshold"}
)

# Playbook integer fields that must be at least 1 (use counts).
_POSITIVE_INTEGERS: frozenset[str] = frozenset({"auto_promote_uses", "prune_min_uses"})

# Playbook fields that must be non-negative.
_NON_NEGATIVE: frozenset[str] = frozenset({"min_resolution_chars", "max_synth_per_hour"})


def _validate_unit_interval(pb: dict[str, Any], keys: frozenset[str]) -> None:
    """Each key, if present, must be a number in [0.0, 1.0]."""
    for key in keys:
        if key not in pb:
            continue
        val = pb[key]
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise ValueError(f"playbooks.{key} must be a number")
        if val < 0.0 or val > 1.0:
            raise ValueError(f"playbooks.{key} must be in [0.0, 1.0]")


def _validate_positive_integers(pb: dict[str, Any], keys: frozenset[str]) -> None:
    """Each key, if present, must be an integer >= 1."""
    for key in keys:
        if key not in pb:
            continue
        val = pb[key]
        if not isinstance(val, int) or isinstance(val, bool):
            raise ValueError(f"playbooks.{key} must be an integer")
        if val < 1:
            raise ValueError(f"playbooks.{key} must be >= 1")


def _validate_non_negative(pb: dict[str, Any], keys: frozenset[str]) -> None:
    """Each key, if present, must be a number >= 0."""
    for key in keys:
        if key not in pb:
            continue
        val = pb[key]
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise ValueError(f"playbooks.{key} must be a number")
        if val < 0:
            raise ValueError(f"playbooks.{key} must be >= 0")


def _validate_playbook_ranges(pb: dict[str, Any]) -> None:
    """Pre-validate PlaybookConfig fields (cleanup batch follow-up).

    Mirrors ``_validate_drone_ranges`` — explicit error messages before
    the generic dispatch so a bad winrate doesn't make it to the
    storage layer where the silent-drop bug class lives. The dashboard
    sliders prevent the common case but the REST endpoint accepts any
    JSON, so this is the only gate.
    """
    _validate_unit_interval(pb, _UNIT_INTERVAL_FLOATS)
    _validate_positive_integers(pb, _POSITIVE_INTEGERS)
    _validate_non_negative(pb, _NON_NEGATIVE)
    # Consolidation floor matches the engine's _playbook_consolidation_loop
    # which floors at 300s anyway; rejecting tiny values prevents the
    # operator from saving a config that the engine then ignores.
    if "consolidation_interval_seconds" in pb:
        val = pb["consolidation_interval_seconds"]
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise ValueError("playbooks.consolidation_interval_seconds must be a number")
        if val < 300:
            raise ValueError("playbooks.consolidation_interval_seconds must be >= 300")


def apply_playbooks(
    cfg: HiveConfig,
    body: dict[str, Any],
    *,
    deps: ApplierDeps,  # protocol-uniform; playbooks doesn't use it
) -> FieldOutcome:
    """Validate and apply the ``playbooks`` section of a config update (P4b).

    PlaybookConfig is all-primitives (no nested dataclasses, no custom
    validation rules beyond the field types themselves) so the generic
    dispatcher handles everything. New fields added to the dataclass
    auto-flow through; unknown body keys are logged + reported in the
    FieldOutcome the same way every other section already does.

    Range checks land before the generic dispatch so explicit contract
    messages win over type errors (cleanup batch).
    """
    _validate_playbook_ranges(body)
    return _apply_dataclass_dict(body, cfg.playbooks, "playbooks")
