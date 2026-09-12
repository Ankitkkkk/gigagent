"""Resolve a provider name to its adapter (spec §6 resolution order)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .base import AmbiguousSessionId, LaunchContext, NullAdapter, ProviderAdapter

ROOT = Path(__file__).resolve().parents[1]
USER_ADAPTERS_DIR = ROOT / "adapters"

# Filled in by providers.claude / providers.codex (Tasks 2 and 3).
_BUILTIN: dict[str, type[ProviderAdapter]] = {}


def register_builtin(name: str, cls: type[ProviderAdapter]) -> None:
    _BUILTIN[name] = cls


def get_adapter(provider_name: str, agent_cfg: dict | None = None) -> ProviderAdapter:
    agent_cfg = dict(agent_cfg or {})
    spec = agent_cfg.get("adapter")
    if spec:
        module_name, sep, class_name = str(spec).partition(":")
        if not sep or not module_name or not class_name:
            raise ValueError(f"adapter must be 'module:Class', got {spec!r}")
        if USER_ADAPTERS_DIR.is_dir() and str(USER_ADAPTERS_DIR) not in sys.path:
            sys.path.insert(0, str(USER_ADAPTERS_DIR))
        cls = getattr(importlib.import_module(module_name), class_name)
        return cls(agent_cfg)
    cls = _BUILTIN.get(provider_name)
    if cls is not None:
        return cls(agent_cfg)
    return NullAdapter(provider_name, agent_cfg)


__all__ = ["AmbiguousSessionId", "LaunchContext", "NullAdapter", "ProviderAdapter",
           "get_adapter", "register_builtin"]

from .claude import ClaudeAdapter  # noqa: E402
register_builtin("claude", ClaudeAdapter)
