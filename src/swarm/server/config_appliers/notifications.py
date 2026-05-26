"""``notifications`` section applier — terminal bell + desktop + webhook + SMTP."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.config.models import EmailConfig, NotifyConfig, WebhookConfig
from swarm.server.config_manager import FieldOutcome, _apply_dataclass_dict

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


_CUSTOM_KEYS: frozenset[str] = frozenset(
    {
        "terminal_bell",
        "desktop",
        "debounce_seconds",
        "desktop_events",
        "terminal_events",
        "templates",
        "webhook",
        "email",
    }
)

_EMAIL_BOOL_KEYS: tuple[str, ...] = ("enabled", "use_tls")
_EMAIL_STRING_KEYS: tuple[str, ...] = ("smtp_host", "smtp_user", "smtp_password", "from_address")


def _validate_string_list(prefix: str, val: object) -> list[str]:
    """Validate that ``val`` is a ``list[str]`` and return it as such."""
    if not isinstance(val, list) or not all(isinstance(e, str) for e in val):
        raise ValueError(f"{prefix} must be a list of strings")
    return list(val)


def _apply_notifications_top_scalars(cfg_n: NotifyConfig, nt: dict[str, Any]) -> None:
    """Validate + assign the top-level NotifyConfig scalars
    (terminal_bell, desktop, debounce_seconds, *_events, templates).

    Extracted from ``apply_notifications`` so the parent stays below
    the C901 complexity threshold.
    """
    for key in ("terminal_bell", "desktop"):
        if key in nt:
            if not isinstance(nt[key], bool):
                raise ValueError(f"notifications.{key} must be boolean")
            setattr(cfg_n, key, nt[key])
    if "debounce_seconds" in nt:
        if not isinstance(nt["debounce_seconds"], (int, float)) or nt["debounce_seconds"] < 0:
            raise ValueError("notifications.debounce_seconds must be >= 0")
        cfg_n.debounce_seconds = nt["debounce_seconds"]
    for key in ("desktop_events", "terminal_events"):
        if key in nt:
            setattr(cfg_n, key, _validate_string_list(f"notifications.{key}", nt[key]))
    if "templates" in nt:
        val = nt["templates"]
        if not isinstance(val, dict):
            raise ValueError("notifications.templates must be an object")
        cfg_n.templates = {str(k): str(v) for k, v in val.items()}


def _apply_notifications_webhook(cfg_wh: WebhookConfig, wh: object) -> None:
    """Validate and apply notifications.webhook sub-section."""
    if not isinstance(wh, dict):
        raise ValueError("notifications.webhook must be an object")
    if "url" in wh:
        if not isinstance(wh["url"], str):
            raise ValueError("notifications.webhook.url must be a string")
        cfg_wh.url = wh["url"].strip()
    if "events" in wh:
        cfg_wh.events = _validate_string_list("notifications.webhook.events", wh["events"])


def _apply_notifications_email_scalars(cfg_em: EmailConfig, em: dict[str, Any]) -> None:
    """Apply email bool/string/int scalar fields."""
    for key in _EMAIL_BOOL_KEYS:
        if key in em:
            if not isinstance(em[key], bool):
                raise ValueError(f"notifications.email.{key} must be boolean")
            setattr(cfg_em, key, em[key])
    for key in _EMAIL_STRING_KEYS:
        if key in em:
            if not isinstance(em[key], str):
                raise ValueError(f"notifications.email.{key} must be a string")
            setattr(cfg_em, key, em[key])
    if "smtp_port" in em:
        val = em["smtp_port"]
        if not isinstance(val, int) or not (1 <= val <= 65535):
            raise ValueError("notifications.email.smtp_port must be 1-65535")
        cfg_em.smtp_port = val


def _apply_notifications_email(cfg_em: EmailConfig, em: object) -> None:
    """Validate and apply notifications.email sub-section."""
    if not isinstance(em, dict):
        raise ValueError("notifications.email must be an object")
    _apply_notifications_email_scalars(cfg_em, em)
    if "to_addresses" in em:
        cfg_em.to_addresses = _validate_string_list(
            "notifications.email.to_addresses", em["to_addresses"]
        )
    if "events" in em:
        cfg_em.events = _validate_string_list("notifications.email.events", em["events"])


def apply_notifications(
    cfg: HiveConfig,
    body: dict[str, Any],
    *,
    deps: ApplierDeps,  # protocol-uniform; notifications doesn't use it
) -> FieldOutcome:
    """Validate and apply the ``notifications`` section of a config update.

    Handles the full ``NotifyConfig`` schema the dashboard sends:
    ``terminal_bell``, ``desktop``, ``debounce_seconds``,
    ``desktop_events``, ``terminal_events``, ``templates``,
    ``webhook.{url,events}``, and the entire ``email`` block.

    Reported in #328 (Bug C): the previous implementation only
    consumed three top-level scalars and silently discarded everything
    else.  Operators editing SMTP settings in the dashboard saw the
    toast "saved" but the values never reached ``save_config_to_db``
    — after a restart the page rendered the defaults again, looking
    like a load-time bug while the actual defect was here in the apply
    path.
    """
    nc = cfg.notifications
    _apply_notifications_top_scalars(nc, body)
    if "webhook" in body:
        _apply_notifications_webhook(nc.webhook, body["webhook"])
    if "email" in body:
        _apply_notifications_email(nc.email, body["email"])
    outcome = _apply_dataclass_dict(body, nc, "notifications", skip_keys=_CUSTOM_KEYS)
    # Custom-validated keys aren't picked up by dispatch's consumed
    # list — populate them by hand so the structured ApplyResult
    # reflects the full set of fields the operator's body successfully
    # applied.
    for key in _CUSTOM_KEYS:
        if key in body:
            outcome.consumed.append(key)
    return outcome
