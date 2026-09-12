"""Claude Code adapter (spec §6). Verified against the installed `claude` on 2026-09-12."""
from __future__ import annotations

import uuid
from pathlib import Path

from .base import ProviderAdapter


class ClaudeAdapter(ProviderAdapter):
    name = "claude"
    supports_resume = True
    can_locate_transcripts = True
    DEFAULT_SUMMARY_MODEL = "haiku"

    def __init__(self, agent_cfg: dict | None = None, home: Path | None = None):
        super().__init__(agent_cfg)
        self.home = Path(home) if home else Path.home()

    def allocate_session_id(self) -> str | None:
        return str(uuid.uuid4())

    def new_session_args(self, session_id: str | None) -> list[str]:
        return ["--session-id", session_id] if session_id else []

    def resume_args(self, session_id: str, cwd: Path) -> list[str]:
        return ["--resume", session_id]

    def locate_transcript(self, session_id: str, cwd: Path) -> Path | None:
        # The project-dir encoding is not relied upon: glob every project.
        matches = sorted((self.home / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
        return matches[0] if matches else None

    def summarizer_command(self, model: str | None, prompt_path: Path,
                           output_path: Path, workdir: Path) -> list[str] | None:
        # Prompt on stdin, summary on stdout. No tools, no MCP, no user settings.
        # Flags confirmed by the Task 0 spike; adjust here if the spike changed them.
        return [
            self.agent_cfg.get("command", "claude"), "-p",
            "--model", model or self.DEFAULT_SUMMARY_MODEL,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
        ]
