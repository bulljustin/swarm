"""``coordination`` section applier — file-ownership + auto-pull settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.server.config_manager import FieldOutcome, _apply_dataclass_dict

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


# Coordination keys with bespoke enum-style validation.  Generic
# dispatch handles ``auto_pull`` (the only pure boolean) and surfaces
# the unknown-sub-key WARNING for drift.
_CUSTOM_KEYS: frozenset[str] = frozenset({"mode", "file_ownership"})


def apply_coordination(
    cfg: HiveConfig,
    body: dict[str, Any],
    *,
    deps: ApplierDeps,  # protocol-uniform; coordination doesn't use it
) -> FieldOutcome:
    """Validate and apply the ``coordination`` section of a config update."""
    cc = cfg.coordination
    consumed_custom: list[str] = []
    if "mode" in body:
        if body["mode"] not in ("single-branch", "worktree"):
            raise ValueError("coordination.mode must be 'single-branch' or 'worktree'")
        cc.mode = body["mode"]
        consumed_custom.append("mode")
    if "file_ownership" in body:
        if body["file_ownership"] not in ("off", "warning", "hard-block"):
            raise ValueError(
                "coordination.file_ownership must be 'off', 'warning', or 'hard-block'"
            )
        cc.file_ownership = body["file_ownership"]
        consumed_custom.append("file_ownership")
    # Generic dispatch covers ``auto_pull`` and emits the
    # unknown-sub-key WARNING for any future drift.
    outcome = _apply_dataclass_dict(body, cc, "coordination", skip_keys=_CUSTOM_KEYS)
    outcome.consumed.extend(consumed_custom)
    return outcome
