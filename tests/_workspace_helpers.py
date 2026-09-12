"""Shared fixtures for the terminal-sessions test files. Not a test module."""
import json
from datetime import datetime, timezone
from pathlib import Path

from providers.base import LaunchContext


def make_launch(agent_id="ag_test", kind="spawn", nonce="n0nce", cwd="/tmp"):
    return LaunchContext(agent_id=agent_id, kind=kind, launch_nonce=nonce,
                         cwd=Path(cwd), launched_at=datetime.now(timezone.utc))


class FakeClock:
    """Injectable clock + sleep: sleeping advances time, nothing blocks."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def write_rollout(home: Path, session_id: str, originator: str, cwd: str, day="2026/09/12"):
    """Seed a fake ~/.codex/sessions rollout whose line 1 is session_meta."""
    d = home / ".codex" / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-09-12T10-00-00-{session_id}.jsonl"
    meta = {"timestamp": "2026-09-12T10:00:00Z", "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd, "originator": originator,
                        "timestamp": "2026-09-12T10:00:00.000Z"}}
    p.write_text(json.dumps(meta) + "\n" + json.dumps({"type": "event"}) + "\n")
    return p


def app_cfg(data_dir, default="none"):
    """Minimal config for app.configure() in tests (copy missing keys from config.toml if needed)."""
    return {
        "server": {"data_dir": str(data_dir), "port": 8300, "host": "127.0.0.1"},
        "agents": {
            "claude": {"command": "claude", "cwd": ".", "color": "#da7756", "label": "Claude"},
            "codex": {"command": "codex", "cwd": ".", "color": "#10a37f", "label": "Codex"},
        },
        "routing": {"default": default, "max_agent_hops": 4},
        "images": {"upload_dir": str(Path(data_dir) / "uploads")},
    }
