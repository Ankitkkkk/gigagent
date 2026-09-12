"""Provider adapter interface — everything provider-specific lives behind this.

Spec §6. Subclasses override only what their provider supports; every method
has a safe default so a minimal adapter is spawn-only and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class LaunchContext:
    """Immutable description of one provider launch (spawn, resume or fresh)."""
    agent_id: str
    kind: str                  # "spawn" | "resume" | "fresh"
    launch_nonce: str          # uuid4 hex, unique per launch
    cwd: Path
    launched_at: datetime
    provider_pid: int | None = None


class AmbiguousSessionId(Exception):
    """More than one transcript matched one launch; nothing is chosen."""

    def __init__(self, count: int):
        super().__init__(f"session id ambiguous ({count} candidates)")
        self.count = count


class ProviderAdapter:
    """Base class with safe defaults."""
    name: str = ""
    supports_resume: bool = False
    can_locate_transcripts: bool = False

    def __init__(self, agent_cfg: dict | None = None):
        self.agent_cfg = dict(agent_cfg or {})

    def allocate_session_id(self) -> str | None:
        """Id to pass on first launch. None = the provider assigns one."""
        return None

    def new_session_args(self, session_id: str | None) -> list[str]:
        """Extra CLI args for a fresh launch. Positionals first, then flags."""
        return []

    def resume_args(self, session_id: str, cwd: Path) -> list[str]:
        raise NotImplementedError(f"{self.name or 'provider'} cannot resume")

    def launch_env(self, launch: LaunchContext) -> dict[str, str]:
        """Extra environment for the provider process."""
        return {}

    def discover_session_id(self, launch: LaunchContext, timeout: float) -> str | None:
        """For providers that assign ids: find it after launch. None on timeout.

        Raises AmbiguousSessionId when more than one candidate matches."""
        return None

    def locate_transcript(self, session_id: str, cwd: Path) -> Path | None:
        return None

    def summarizer_command(self, model: str | None, prompt_path: Path,
                           output_path: Path, workdir: Path) -> list[str] | None:
        """Headless summarise command (spec §3), or None if not tool-free."""
        return None


class NullAdapter(ProviderAdapter):
    """Spawn-only adapter for providers without one of their own."""

    def __init__(self, name: str, agent_cfg: dict | None = None):
        super().__init__(agent_cfg)
        self.name = name
