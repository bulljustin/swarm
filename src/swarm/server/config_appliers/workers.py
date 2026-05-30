"""``workers`` and top-level scalar appliers — worker identity, providers, buttons, graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.config import WorkerConfig
from swarm.server.config_manager import FieldOutcome

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


# Top-level keys consumed by ``apply_scalars``.  Used to derive the
# fail-loud guard's known-keys set in apply_update.
SCALARS_KEYS: frozenset[str] = frozenset(
    {
        "workers",
        "provider",
        "default_group",
        "session_name",
        "projects_dir",
        "log_level",
        "graph_client_id",
        "graph_tenant_id",
        "graph_client_secret",
        "tool_buttons",
        "action_buttons",
        "task_buttons",
    }
)


def _apply_worker_identity(
    cfg: HiveConfig,
    wc: WorkerConfig,
    wdata: dict[str, Any],
    original_name: str,
    *,
    deps: ApplierDeps,
) -> None:
    """Apply name/path changes to a worker config and sync live worker."""
    from swarm.server.helpers import validate_worker_name

    live_name: str | None = None
    live_path: str | None = None

    if "name" in wdata and isinstance(wdata["name"], str):
        new_name = wdata["name"].strip()
        if new_name and new_name != wc.name:
            if err := validate_worker_name(new_name):
                raise ValueError(err)
            if cfg.get_worker(new_name):
                raise ValueError(f"Cannot rename '{wc.name}' to '{new_name}': already exists")
            live_name = new_name
            wc.name = new_name
    if "path" in wdata and isinstance(wdata["path"], str):
        new_path = wdata["path"].strip()
        if new_path and new_path != wc.path:
            live_path = new_path
            wc.path = new_path

    if live_name or live_path:
        svc = deps.get_worker_svc()
        if svc and svc.get_worker(original_name):
            svc.update_worker(original_name, name=live_name, path=live_path)


def _apply_worker_entry(
    cfg: HiveConfig,
    wc: WorkerConfig,
    wdata: dict[str, Any],
    valid_providers: set[str],
    original_name: str,
    *,
    deps: ApplierDeps,
) -> None:
    """Apply a single worker's config update (description, provider, name, path)."""
    if "description" in wdata and isinstance(wdata["description"], str):
        wc.description = wdata["description"]
    if "provider" in wdata:
        prov = wdata["provider"] if isinstance(wdata["provider"], str) else ""
        if prov and prov not in valid_providers:
            raise ValueError(f"Worker '{wc.name}' has invalid provider '{prov}'")
        wc.provider = prov
    _apply_worker_identity(cfg, wc, wdata, original_name, deps=deps)


def _apply_workers(cfg: HiveConfig, workers: dict[str, Any], *, deps: ApplierDeps) -> None:
    """Validate and apply worker descriptions, providers, names, and paths."""
    from swarm.providers import get_valid_providers

    valid = get_valid_providers()
    for wname, wdata in workers.items():
        wc = cfg.get_worker(wname)
        if not wc:
            continue
        if isinstance(wdata, str):
            wc.description = wdata
        elif isinstance(wdata, dict):
            _apply_worker_entry(cfg, wc, wdata, valid, wname, deps=deps)


def _apply_default_group(cfg: HiveConfig, dg: object) -> None:
    """Validate and apply default_group setting."""
    if not isinstance(dg, str):
        raise ValueError("default_group must be a string")
    if dg:
        group_names = {g.name.lower() for g in cfg.groups}
        if dg.lower() not in group_names:
            raise ValueError(f"default_group '{dg}' does not match any defined group")
    cfg.default_group = dg


def _apply_buttons(cfg: HiveConfig, body: dict[str, Any]) -> None:
    """Apply tool_buttons and action_buttons from the request body."""
    if "tool_buttons" in body and isinstance(body["tool_buttons"], list):
        from swarm.config import ToolButtonConfig

        cfg.tool_buttons = [
            ToolButtonConfig(label=b["label"], command=b.get("command", ""))
            for b in body["tool_buttons"]
            if isinstance(b, dict) and b.get("label")
        ]
    if "action_buttons" in body and isinstance(body["action_buttons"], list):
        from swarm.config import ActionButtonConfig

        cfg.action_buttons = [
            ActionButtonConfig(
                label=b["label"],
                action=b.get("action", ""),
                command=b.get("command", ""),
                style=b.get("style", "secondary"),
                show_mobile=b.get("show_mobile", True),
                show_desktop=b.get("show_desktop", True),
            )
            for b in body["action_buttons"]
            if isinstance(b, dict) and b.get("label")
        ]
    if "queen_action_buttons" in body and isinstance(body["queen_action_buttons"], list):
        from swarm.config import QueenActionButtonConfig

        cfg.queen_action_buttons = [
            QueenActionButtonConfig(
                label=b["label"],
                action=b.get("action", "send"),
                value=b.get("value", ""),
                style=b.get("style", "secondary"),
                show_mobile=b.get("show_mobile", True),
                show_desktop=b.get("show_desktop", True),
            )
            for b in body["queen_action_buttons"]
            if isinstance(b, dict) and b.get("label")
        ]
    if "task_buttons" in body and isinstance(body["task_buttons"], list):
        from swarm.config import TaskButtonConfig

        cfg.task_buttons = [
            TaskButtonConfig(
                label=b["label"],
                action=b.get("action", ""),
                show_mobile=b.get("show_mobile", True),
                show_desktop=b.get("show_desktop", True),
            )
            for b in body["task_buttons"]
            if isinstance(b, dict) and b.get("label") and b.get("action")
        ]


def apply_scalars(
    cfg: HiveConfig,
    body: dict[str, Any],
    *,
    deps: ApplierDeps,
) -> FieldOutcome:
    """Apply workers, default_group, scalars, and graph settings.

    "Virtual section" — its keys live at the top level of the request
    body rather than under a dedicated section name.  The orchestrator
    feeds the whole ``body`` in.
    """
    from swarm.providers import get_valid_providers

    consumed: list[str] = []
    valid = get_valid_providers()
    if "workers" in body and isinstance(body["workers"], dict):
        _apply_workers(cfg, body["workers"], deps=deps)
        consumed.append("workers")
    if "provider" in body:
        prov = body["provider"]
        if isinstance(prov, str) and prov in valid:
            cfg.provider = prov
            consumed.append("provider")
        elif prov:
            raise ValueError(f"Invalid global provider '{prov}'")
    if "default_group" in body:
        _apply_default_group(cfg, body["default_group"])
        consumed.append("default_group")
    for key in ("session_name", "projects_dir", "log_level"):
        if key in body:
            setattr(cfg, key, body[key])
            consumed.append(key)
    for key, attr, default in (
        ("graph_client_id", "graph_client_id", ""),
        ("graph_tenant_id", "graph_tenant_id", "common"),
        ("graph_client_secret", "graph_client_secret", ""),
    ):
        if key in body and isinstance(body[key], str):
            val = body[key].strip() or default
            setattr(cfg, attr, val)
            consumed.append(key)
    for key in ("tool_buttons", "action_buttons", "queen_action_buttons", "task_buttons"):
        if key in body:
            consumed.append(key)
    _apply_buttons(cfg, body)
    return FieldOutcome(consumed=consumed)
