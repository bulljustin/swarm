"""``llms`` and ``provider_overrides`` section appliers — custom + builtin provider tuning."""

from __future__ import annotations

import re as _re
from typing import TYPE_CHECKING

from swarm.server.config_manager import FieldOutcome

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


def _parse_tuning_from_entry(prefix: str, entry: dict[str, object]) -> object:
    """Parse and validate ProviderTuning fields from a dict."""
    from swarm.config import _TUNING_FIELDS, ProviderTuning, _parse_tuning

    if not any(entry.get(k) for k in _TUNING_FIELDS):
        return ProviderTuning()
    tuning = _parse_tuning(entry)
    # Validate regex patterns
    for field_name in (
        "idle_pattern",
        "busy_pattern",
        "choice_pattern",
        "user_question_pattern",
        "safe_patterns",
    ):
        val = getattr(tuning, field_name, "")
        if val:
            try:
                _re.compile(val)
            except _re.error as exc:
                raise ValueError(f"{prefix}.{field_name}: invalid regex: {exc}") from exc
    return tuning


def apply_llms(
    cfg: HiveConfig,
    body: object,
    *,
    deps: ApplierDeps,
) -> FieldOutcome:
    """Validate and apply custom LLM providers from a config update.

    Calls back into ``deps.invalidate_provider_cache`` so the pilot
    drops its cached :class:`~swarm.providers.LLMProvider` instances
    and re-resolves them against the new tuning on the next poll.
    """
    if not isinstance(body, list):
        raise ValueError("llms must be a list")
    from swarm.config import CustomLLMConfig
    from swarm.providers import ProviderType, register_custom_providers

    builtin = frozenset(p.value for p in ProviderType)
    parsed: list[CustomLLMConfig] = []
    seen: set[str] = set()
    for i, entry in enumerate(body):
        if not isinstance(entry, dict):
            raise ValueError(f"llms[{i}] must be an object")
        name = entry.get("name", "").strip()
        if not name:
            raise ValueError(f"llms[{i}]: name is required")
        if name in builtin:
            raise ValueError(f"llms[{i}]: name '{name}' collides with built-in provider")
        if name in seen:
            raise ValueError(f"llms[{i}]: duplicate name '{name}'")
        seen.add(name)
        command = entry.get("command", [])
        if isinstance(command, str):
            command = command.split()
        if not command:
            raise ValueError(f"llms[{i}]: command is required")
        display_name = entry.get("display_name", "").strip()
        tuning = _parse_tuning_from_entry(f"llms[{i}]", entry)
        parsed.append(
            CustomLLMConfig(
                name=name,
                command=command,
                display_name=display_name,
                tuning=tuning,
            )
        )
    cfg.custom_llms = parsed
    register_custom_providers(parsed)
    deps.invalidate_provider_cache()
    return FieldOutcome()


def apply_provider_overrides(
    cfg: HiveConfig,
    body: object,
    *,
    deps: ApplierDeps,
) -> FieldOutcome:
    """Validate and apply provider tuning overrides for built-in providers.

    Calls back into ``deps.invalidate_provider_cache`` to drop the
    pilot's cache so overrides take effect immediately.
    """
    if not isinstance(body, dict):
        raise ValueError("provider_overrides must be an object")
    from swarm.config import ProviderTuning
    from swarm.providers import get_valid_providers, register_provider_overrides

    valid = get_valid_providers()
    parsed: dict[str, ProviderTuning] = {}
    for pname, pdata in body.items():
        if pname not in valid:
            raise ValueError(f"provider_overrides: unknown provider '{pname}'")
        if not isinstance(pdata, dict):
            raise ValueError(f"provider_overrides.{pname} must be an object")
        tuning = _parse_tuning_from_entry(f"provider_overrides.{pname}", pdata)
        if tuning.has_tuning():
            parsed[pname] = tuning
    cfg.provider_overrides = parsed
    register_provider_overrides(parsed)
    deps.invalidate_provider_cache()
    return FieldOutcome()
