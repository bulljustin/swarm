"""``workflows`` section applier — task-type → skill-name mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.server.config_manager import FieldOutcome

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


_VALID_TYPES = frozenset({"bug", "feature", "verify", "chore"})


def apply_workflows(
    cfg: HiveConfig,
    body: object,
    *,
    deps: ApplierDeps,  # protocol-uniform; workflows doesn't use it
) -> FieldOutcome:
    """Validate and apply the ``workflows`` section of a config update.

    **Empty body is a no-op** — the dashboard's ``saveSettings`` always
    includes a ``workflows`` key built by reading the four
    Automation-tab inputs.  When those inputs are empty (e.g. because
    the user's editing a group on a different tab and the workflow
    fields rendered blank), the body carries ``workflows: {}``.
    Pre-fix this wiped ``cfg.workflows`` in-memory; ``serialize_config``
    then dropped the key from the DB write, so the row was preserved
    on disk but the running daemon's state was stale until the next
    restart.  Operators saw "I typed /verify, saved, restarted, it's
    gone" because every unrelated config save in between cleared the
    in-memory dict.

    Same destructive-empty-overwrite footgun the approval_rules table
    had pre-#328.  Apply the same guard: only overwrite when the body
    genuinely carries entries.  Explicit clearing is a future
    enhancement (separate endpoint).
    """
    if not isinstance(body, dict):
        raise ValueError("workflows must be an object")
    if not body:
        return FieldOutcome()
    cleaned: dict[str, str] = {}
    consumed: list[str] = []
    for k, v in body.items():
        if k not in _VALID_TYPES:
            raise ValueError(f"workflows key '{k}' is not a valid task type")
        if not isinstance(v, str):
            raise ValueError(f"workflows.{k} must be a string")
        cleaned[k] = v.strip()
        consumed.append(k)
    cfg.workflows = cleaned
    from swarm.tasks.workflows import apply_config_overrides

    apply_config_overrides(cleaned)
    return FieldOutcome(consumed=consumed)
