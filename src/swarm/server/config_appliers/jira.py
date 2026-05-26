"""``jira`` section applier — Jira sync config."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.server.config_manager import FieldOutcome, _resolve_hints, _warn_unknown_subkeys

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


_JIRA_STRING_KEYS: tuple[str, ...] = (
    "project",
    "import_filter",
    "import_label",
    "client_id",
    "client_secret",
    "cloud_id",
)


def _apply_jira_strings(cfg: object, jr: dict[str, Any], keys: tuple[str, ...]) -> None:
    """Validate and apply string fields on the Jira config."""
    for key in keys:
        if key in jr:
            if not isinstance(jr[key], str):
                raise ValueError(f"jira.{key} must be a string")
            val = jr[key].strip()
            if not val and key in ("client_id", "client_secret"):
                continue
            setattr(cfg, key, val)


def apply_jira(
    cfg: HiveConfig,
    body: dict[str, Any],
    *,
    deps: ApplierDeps,  # protocol-uniform; jira doesn't use it
) -> FieldOutcome:
    """Validate and apply the ``jira`` section of a config update.

    Every JiraConfig field has bespoke validation (regex-shape
    client_id, range-checked sync_interval, default-merged status_map,
    empty-string fallbacks for credentials) so the body of this
    handler stays hand-coded.  Phase 7 instruments it to track
    consumed keys and emit the standard unknown-sub-key WARNING via
    the generic dispatch sweep.
    """
    from swarm.config import JiraConfig

    jc = cfg.jira
    consumed: list[str] = []
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise ValueError("jira.enabled must be boolean")
        jc.enabled = body["enabled"]
        consumed.append("enabled")
    for key in _JIRA_STRING_KEYS:
        if key in body:
            consumed.append(key)
    _apply_jira_strings(jc, body, _JIRA_STRING_KEYS)
    if "sync_interval_minutes" in body:
        val = body["sync_interval_minutes"]
        if not isinstance(val, (int, float)) or val <= 0:
            raise ValueError("jira.sync_interval_minutes must be > 0")
        jc.sync_interval_minutes = float(val)
        consumed.append("sync_interval_minutes")
    if "lookback_days" in body:
        val = body["lookback_days"]
        if not isinstance(val, (int, float)) or val < 0:
            raise ValueError("jira.lookback_days must be >= 0")
        jc.lookback_days = int(val)
        consumed.append("lookback_days")
    if "status_map" in body:
        val = body["status_map"]
        if not isinstance(val, dict):
            raise ValueError("jira.status_map must be an object")
        # Merge with defaults so empty {} doesn't wipe all mappings
        default_map = {
            "backlog": "To Do",
            "unassigned": "To Do",
            "assigned": "To Do",
            "active": "In Progress",
            "done": "Done",
            "failed": "To Do",
        }
        jc.status_map = {**default_map, **{str(k): str(v) for k, v in val.items()}}
        consumed.append("status_map")
    # Drift sweep — every JiraConfig field is custom-handled above, so
    # dispatch only fires for unknown sub-keys.
    outcome = FieldOutcome(consumed=list(consumed))
    _warn_unknown_subkeys(body, JiraConfig, "jira")
    # Compute unknown via the same field set the warn helper uses.
    declared = set(_resolve_hints(JiraConfig).keys())
    outcome.unknown = sorted(set(body) - declared)
    return outcome
