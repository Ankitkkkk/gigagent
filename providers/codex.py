"""Codex CLI adapter (spec §6). Verified against codex 0.154.0 on 2026-09-12.

Codex assigns the conversation id itself and writes it to
~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl, whose first line is
{"type": "session_meta", "payload": {"id", "cwd", "originator", ...}}.
`originator` comes from CODEX_INTERNAL_ORIGINATOR_OVERRIDE, so a launch-specific
value there is the correlation key. cwd and timestamp are never used to match.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .base import AmbiguousSessionId, LaunchContext, ProviderAdapter

ORIGINATOR_ENV = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"


def originator_for(launch: LaunchContext) -> str:
    return f"agentchattr:{launch.agent_id}:{launch.launch_nonce}"


class CodexAdapter(ProviderAdapter):
    name = "codex"
    supports_resume = True
    can_locate_transcripts = True
    POLL_INTERVAL = 2.0

    def __init__(self, agent_cfg: dict | None = None, home: Path | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        super().__init__(agent_cfg)
        self.home = Path(home) if home else Path.home()
        self._sleep = sleep
        self._clock = clock

    @property
    def sessions_dir(self) -> Path:
        return self.home / ".codex" / "sessions"

    def launch_env(self, launch: LaunchContext) -> dict[str, str]:
        return {ORIGINATOR_ENV: originator_for(launch)}

    def resume_args(self, session_id: str, cwd: Path) -> list[str]:
        return ["resume", "-C", str(cwd), session_id]

    def locate_transcript(self, session_id: str, cwd: Path) -> Path | None:
        matches = sorted(self.sessions_dir.glob(f"**/rollout-*-{session_id}.jsonl"))
        return matches[0] if matches else None

    def _candidates(self, originator: str) -> list[str]:
        found: set[str] = set()
        if not self.sessions_dir.is_dir():
            return []
        for path in self.sessions_dir.glob("**/rollout-*.jsonl"):
            try:
                with open(path, encoding="utf-8") as f:
                    payload = json.loads(f.readline()).get("payload", {})
            except (OSError, ValueError):
                continue
            if payload.get("originator") == originator and payload.get("id"):
                found.add(str(payload["id"]))
        return sorted(found)

    def discover_session_id(self, launch: LaunchContext, timeout: float) -> str | None:
        originator = originator_for(launch)
        deadline = self._clock() + timeout
        while True:
            ids = self._candidates(originator)
            if len(ids) == 1:
                return ids[0]
            if len(ids) > 1:
                raise AmbiguousSessionId(len(ids))
            if self._clock() >= deadline:
                return None
            self._sleep(self.POLL_INTERVAL)

    def summarizer_command(self, model: str | None, prompt_path: Path,
                           output_path: Path, workdir: Path) -> list[str] | None:
        # No proven tool-free exec mode (spec §3). Task 0 may change this.
        return None
