"""``advanced`` virtual section applier — top-level port/trust_proxy/domain/terminal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.server.config_manager import FieldOutcome, _apply_dataclass_dict

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


# Top-level keys consumed by ``apply_advanced``.  Used to derive the
# fail-loud guard's known-keys set in apply_update.
ADVANCED_KEYS: frozenset[str] = frozenset(
    {"port", "trust_proxy", "tunnel_domain", "domain", "terminal"}
)


def _apply_advanced_bools(cfg: HiveConfig, body: dict[str, Any]) -> None:
    """Apply boolean advanced fields from the config body."""
    for key in ("trust_proxy",):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(f"{key} must be boolean")
            setattr(cfg, key, body[key])


def apply_advanced(
    cfg: HiveConfig,
    body: dict[str, Any],
    *,
    deps: ApplierDeps,  # protocol-uniform; advanced doesn't use it
) -> FieldOutcome:
    """Apply top-level advanced fields.

    Phase 7: tracks consumed keys for the structured ApplyResult and
    recurses into ``terminal`` via generic dispatch so its
    unknown-sub-key warning fires on drift.
    """
    consumed: list[str] = []
    if "port" in body:
        val = body["port"]
        if not isinstance(val, int) or not (1024 <= val <= 65535):
            raise ValueError("port must be integer between 1024 and 65535")
        cfg.port = val
        consumed.append("port")
    _apply_advanced_bools(cfg, body)
    if "trust_proxy" in body:
        consumed.append("trust_proxy")
    if "tunnel_domain" in body:
        if not isinstance(body["tunnel_domain"], str):
            raise ValueError("tunnel_domain must be a string")
        cfg.tunnel_domain = body["tunnel_domain"].strip()
        consumed.append("tunnel_domain")
    if "domain" in body:
        if not isinstance(body["domain"], str):
            raise ValueError("domain must be a string")
        cfg.domain = body["domain"].strip()
        consumed.append("domain")
    if "terminal" in body and isinstance(body["terminal"], dict):
        # Generic dispatch validates terminal sub-keys and warns on
        # unknowns; terminal only has 1 active field so the explicit
        # branch below is now redundant but kept for backwards-compat
        # error messages.
        t = body["terminal"]
        if "replay_scrollback" in t:
            if not isinstance(t["replay_scrollback"], bool):
                raise ValueError("terminal.replay_scrollback must be boolean")
            cfg.terminal.replay_scrollback = t["replay_scrollback"]
        _apply_dataclass_dict(t, cfg.terminal, "terminal", skip_keys={"replay_scrollback"})
        consumed.append("terminal")
    return FieldOutcome(consumed=consumed)
