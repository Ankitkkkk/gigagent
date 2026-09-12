"""Server-owned process control for workspace agents (spec §2, §4, §6, §7)."""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from providers import AmbiguousSessionId, LaunchContext, get_adapter
from registry import NameInUse
from workspace_store import HISTORY_MODES
from workspace_unread import bundle_prompt, unread

log = logging.getLogger(__name__)

READY_TIMEOUT = 60.0
DISCOVERY_TIMEOUT = 60.0
VERIFY_DELAY = 10.0
LOG_TAIL_LINES = 20

LITERAL_PROMPT = ("Catch up: use mcp to read #{channel} with since_id=-1 and keep reading while "
                  "has_more is true, then respond in #{channel} with a two-line status of where "
                  "things stand.")


class LaunchError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class TmuxOps:
    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def has_session(self, name: str) -> bool:
        try:
            return subprocess.run(
                ["tmux", "has-session", "-t", name], capture_output=True, timeout=5
            ).returncode == 0
        except Exception:
            return False

    def kill_session(self, name: str) -> None:
        try:
            subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, timeout=5)
        except Exception:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _daemon_background(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


class WorkspaceLauncher:
    def __init__(self, *, store, messages, registry, agents, config: dict, data_dir: Path, root: Path,
                 popen=subprocess.Popen, tmux=None, kill=os.kill, clock=time.time, sleep=time.sleep,
                 python=sys.executable, adapters=get_adapter, which=shutil.which, background=None):
        self.store = store
        self.messages = messages
        self.registry = registry
        self.agents = agents
        self.config = config
        self.data_dir = Path(data_dir)
        self.root = Path(root)
        self._popen = popen
        self._tmux = tmux or TmuxOps()
        self._kill = kill
        self._clock = clock
        self._sleep = sleep
        self._python = python
        self._adapters = adapters
        self._which = which
        self._background = background or _daemon_background
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    def tmux_name(self, agent: dict) -> str:
        return f"agentchattr-{agent['agent_id']}"

    def _adapter(self, provider: str):
        return self._adapters(provider, self.config.get("agents", {}).get(provider, {}))

    def _channel_latest_id(self, channel: str) -> int:
        recent = self.messages.get_recent(1, channel=channel)
        return recent[-1]["id"] if recent else -1

    def _floor_for(self, mode: str, channel: str) -> int:
        return 0 if mode == "literal" else self._channel_latest_id(channel) + 1

    def _validate(self, ws: dict | None, provider: str, cwd: str) -> None:
        if self.store.warning:
            raise LaunchError(
                409,
                f"session store is in recovery: {self.store.warning}. Restore or remove the "
                "quarantined file, then restart the server before spawning or resuming agents.",
            )
        if ws is None:
            raise LaunchError(404, "session not found")
        if ws.get("archived"):
            raise LaunchError(400, "session is archived; unarchive it first")
        agents_cfg = self.config.get("agents", {})
        if provider not in agents_cfg:
            known = ", ".join(sorted(agents_cfg))
            raise LaunchError(400, f"unknown provider '{provider}'; known: {known}")
        path = Path(cwd)
        if not path.is_absolute():
            raise LaunchError(400, f"cwd must be an absolute path, got {cwd!r}")
        if not path.is_dir():
            raise LaunchError(400, f"cwd does not exist or is not a directory: {cwd}")
        command = agents_cfg[provider].get("command", provider)
        if not self._which(command):
            raise LaunchError(400, f"'{command}' is not on PATH; install it first")
        if not self._tmux.available():
            raise LaunchError(400, "tmux is required to run agents in the background (Linux/macOS)")

    def launch_context_for(self, agent: dict) -> LaunchContext:
        last_launch = agent.get("last_launch") or {}
        try:
            launched_at = datetime.strptime(
                last_launch.get("at", ""), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            launched_at = datetime.now(timezone.utc)
        return LaunchContext(
            agent_id=agent["agent_id"],
            kind=last_launch.get("kind", "spawn"),
            launch_nonce=last_launch.get("nonce", ""),
            cwd=Path(agent["cwd"]),
            launched_at=launched_at,
            provider_pid=last_launch.get("pid"),
        )

    def _wrapper_command(self, ws: dict, agent: dict, provider_args: list[str],
                         env: dict[str, str]) -> list[str]:
        server = self.config.get("server", {})
        mcp = self.config.get("mcp", {})
        cmd = [
            self._python, str(self.root / "wrapper.py"), agent["provider"],
            "--no-attach", "--no-restart", "--cwd", agent["cwd"],
            "--identity-file", str(self.store.identity_path(agent["agent_id"])),
            "--tmux-name", self.tmux_name(agent), "--data-dir", str(self.data_dir),
            "--port", str(server.get("port", 8300)),
            "--mcp-http-port", str(mcp.get("http_port", 8200)),
            "--mcp-sse-port", str(mcp.get("sse_port", 8201)),
        ]
        for key, value in env.items():
            cmd += ["--provider-env", f"{key}={value}"]
        return cmd + list(provider_args)

    def _launch(self, ws: dict, agent: dict, provider_args: list[str], env: dict[str, str]) -> int:
        logs = self.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_file = open(logs / f"wrapper-{agent['agent_id']}.log", "w", encoding="utf-8")
        cmd = self._wrapper_command(ws, agent, provider_args, env)
        try:
            proc = self._popen(
                cmd, cwd=str(self.root), stdout=log_file, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()
        with self._lock:
            self._pending[agent["agent_id"]] = {"ws_id": ws["id"], "started": self._clock()}
        return proc.pid

    def _log_tail(self, agent_id: str) -> str:
        path = self.data_dir / "logs" / f"wrapper-{agent_id}.log"
        try:
            return "\n".join(path.read_text("utf-8", errors="replace").splitlines()[-LOG_TAIL_LINES:])
        except OSError:
            return ""

    def _register(self, ws: dict, provider: str, preferred: str, custom: bool,
                  allow_reserved: bool = False) -> dict:
        try:
            return self.registry.register(
                provider, label=f"{ws['name']} {provider}", preferred_name=preferred,
                allow_reserved=allow_reserved,
            )
        except NameInUse as exc:
            if custom:
                raise LaunchError(400, f"name in use: {exc.name}")
            raise LaunchError(
                409, f"name {exc.name} in use; stop that agent or resume with --name <new>"
            )

    def spawn(self, ws_id: str, provider: str, cwd: str, history_mode: str,
              name: str | None = None) -> dict:
        ws = self.store.get(ws_id)
        self._validate(ws, provider, cwd)
        if history_mode not in HISTORY_MODES:
            raise LaunchError(400, f"history_mode must be one of {', '.join(HISTORY_MODES)}")
        if history_mode == "summary":
            raise LaunchError(400, "summary history mode is not available in this version; use literal or none")
        saved = set(self.store.member_names())
        if name and name in saved:
            raise LaunchError(400, f"name in use by a saved agent: {name}")
        preferred = name or self.registry.free_slot_name(provider, exclude=saved)
        reg = None
        agent = None
        try:
            reg = self._register(ws, provider, preferred, custom=bool(name))
            adapter = self._adapter(provider)
            session_id = adapter.allocate_session_id()
            last_launch = {
                "kind": "spawn", "nonce": uuid.uuid4().hex, "at": _now_iso(), "pid": None,
            }
            agent = self.store.add_agent(
                ws_id, provider=provider, cwd=str(Path(cwd).resolve()), history_mode=history_mode,
                registry_name=reg["name"], floor_id=self._floor_for(history_mode, ws["channel"]),
                native_session_id=session_id,
                history_state="done" if history_mode == "none" else "pending",
                last_launch=last_launch,
            )
            self.store.write_identity(ws, agent, reg["token"])
            launch = self.launch_context_for(agent)
            wrapper_pid = self._launch(
                ws, agent, adapter.new_session_args(session_id), adapter.launch_env(launch)
            )
        except Exception as exc:
            if reg is not None:
                self.registry.deregister(reg["name"])
            if agent is not None:
                self.store.delete_identity(agent["agent_id"])
                self.store.remove_agent(ws_id, agent["agent_id"])
            if isinstance(exc, LaunchError):
                raise
            raise LaunchError(500, f"failed to start wrapper: {exc}")
        last_launch["wrapper_pid"] = wrapper_pid
        return self.store.update_agent(ws_id, agent["agent_id"], last_launch=last_launch)

    def resume(self, ws_id: str, agent_id: str, fresh: bool = False,
               name: str | None = None, cwd: str | None = None) -> dict:
        ws = self.store.get(ws_id)
        agent = self.store.get_agent(ws_id, agent_id) if ws else None
        if agent is None:
            raise LaunchError(404, "agent not found")
        requested_cwd = cwd if cwd is not None else agent["cwd"]
        self._validate(ws, agent["provider"], requested_cwd)
        effective_cwd = str(Path(requested_cwd).resolve())
        if agent["last_state"] in ("starting", "running") or self._tmux.has_session(self.tmux_name(agent)):
            raise LaunchError(409, f"{agent['registry_name']} is already running")
        adapter = self._adapter(agent["provider"])
        session_id = agent["native_session_id"]
        if not fresh:
            if not adapter.supports_resume:
                raise LaunchError(
                    409, f"resume not supported for {agent['provider']}; add an adapter "
                    "or resume with --fresh"
                )
            if session_id is None:
                raise LaunchError(
                    409, f"{agent['registry_name']} has no saved conversation id; "
                    "resume with --fresh to start a new conversation"
                )
            if adapter.can_locate_transcripts and adapter.locate_transcript(
                session_id, Path(effective_cwd)
            ) is None:
                raise LaunchError(
                    409, f"transcript for {session_id} not found on disk; the conversation may have "
                    "been deleted or moved. Resume with --fresh to start a new one"
                )
        restore = {
            key: agent[key]
            for key in (
                "registry_name", "cwd", "previous_cwds", "native_session_id",
                "previous_native_ids", "native_verified", "history_state", "last_launch",
            )
        }
        original_identity = self.store.read_identity(agent_id)
        preferred = name or agent["registry_name"]
        if name and name != agent["registry_name"] and name in set(self.store.member_names()):
            raise LaunchError(400, f"name in use by a saved agent: {name}")
        reg = None
        try:
            reg = self._register(
                ws, agent["provider"], preferred, custom=False,
                allow_reserved=(preferred == agent["registry_name"]),
            )
            fields = {"registry_name": reg["name"], "last_state": "starting", "last_error": None}
            if effective_cwd != agent["cwd"]:
                fields["cwd"] = effective_cwd
                fields["previous_cwds"] = agent.get("previous_cwds", []) + [agent["cwd"]]
            kind = "fresh" if fresh else "resume"
            if fresh:
                new_session_id = adapter.allocate_session_id()
                fields["previous_native_ids"] = agent.get("previous_native_ids", []) + (
                    [session_id] if session_id else []
                )
                fields["native_session_id"] = new_session_id
                fields["native_verified"] = False
                if agent["history_mode"] == "literal":
                    fields["history_state"] = "pending"
                session_id = new_session_id
            fields["last_launch"] = {
                "kind": kind, "nonce": uuid.uuid4().hex, "at": _now_iso(), "pid": None,
            }
            agent = self.store.update_agent(ws_id, agent_id, **fields)
            self.store.write_identity(ws, agent, reg["token"])
            launch = self.launch_context_for(agent)
            provider_args = (
                adapter.new_session_args(session_id)
                if fresh else adapter.resume_args(session_id, Path(agent["cwd"]))
            )
            wrapper_pid = self._launch(ws, agent, provider_args, adapter.launch_env(launch))
        except Exception as exc:
            if reg is not None:
                self.registry.deregister(reg["name"])
                self.store.update_agent(
                    ws_id, agent_id, last_state="exited",
                    last_error=f"failed to start wrapper: {exc}", **restore,
                )
                self.store.restore_identity(agent_id, original_identity)
            if isinstance(exc, LaunchError):
                raise
            raise LaunchError(500, f"failed to start wrapper: {exc}")
        last_launch = dict(agent["last_launch"])
        last_launch["wrapper_pid"] = wrapper_pid
        return self.store.update_agent(ws_id, agent_id, last_launch=last_launch)

    def on_heartbeat(self, registry_name: str, ready: bool, pid: int | None) -> None:
        found = self.store.find_agent_by_registry_name(registry_name)
        if not found:
            return
        ws, agent = found
        if agent["last_state"] != "starting" or not ready:
            return
        last_launch = dict(agent["last_launch"])
        last_launch["pid"] = pid
        self.store.update_agent(
            ws["id"], agent["agent_id"], last_state="running", last_launch=last_launch
        )
        with self._lock:
            self._pending.pop(agent["agent_id"], None)
        self._background(self._after_ready, ws["id"], agent["agent_id"], last_launch.get("nonce"))

    def _update_if_launch(self, ws_id: str, agent_id: str, nonce: str, **fields) -> bool:
        ok = self.store.update_agent_if_launch(ws_id, agent_id, nonce, **fields)
        if not ok:
            log.info("dropping stale background result for %s (launch changed)", agent_id)
        return ok

    def _after_ready(self, ws_id: str, agent_id: str, nonce: str) -> None:
        ws = self.store.get(ws_id)
        agent = self.store.get_agent(ws_id, agent_id)
        if not ws or not agent or (agent.get("last_launch") or {}).get("nonce") != nonce:
            return
        kind = agent["last_launch"].get("kind", "spawn")
        if agent["history_mode"] == "literal" and agent["history_state"] == "pending":
            if not self._update_if_launch(ws_id, agent_id, nonce, history_state="done"):
                return
            try:
                self.agents.trigger_sync(
                    agent["registry_name"], message="catch up", channel=ws["channel"],
                    prompt=LITERAL_PROMPT.format(channel=ws["channel"]),
                )
            except Exception:
                self._update_if_launch(ws_id, agent_id, nonce, history_state="pending")
                log.exception("failed to enqueue literal catch-up for %s", agent_id)
                return
        if kind in ("resume", "fresh"):
            self._send_bundle(ws, self.store.get_agent(ws_id, agent_id))
        adapter = self._adapter(agent["provider"])
        agent = self.store.get_agent(ws_id, agent_id)
        if agent["native_session_id"] is None:
            try:
                session_id = adapter.discover_session_id(
                    self.launch_context_for(agent), DISCOVERY_TIMEOUT
                )
            except AmbiguousSessionId as exc:
                self._update_if_launch(ws_id, agent_id, nonce, history_note=str(exc))
                return
            if session_id is None:
                self._update_if_launch(
                    ws_id, agent_id, nonce,
                    history_note=f"{agent['provider']} session id not found",
                )
                return
            if not self._update_if_launch(
                ws_id, agent_id, nonce, native_session_id=session_id
            ):
                return
        else:
            self._sleep(VERIFY_DELAY)
        self._verify_transcript(ws_id, agent_id, adapter, nonce)

    def _verify_transcript(self, ws_id: str, agent_id: str, adapter,
                           nonce: str | None = None) -> None:
        agent = self.store.get_agent(ws_id, agent_id)
        if not agent or not agent["native_session_id"] or not adapter.can_locate_transcripts:
            return
        found = adapter.locate_transcript(
            agent["native_session_id"], Path(agent["cwd"])
        ) is not None
        if nonce is None:
            self.store.update_agent(ws_id, agent_id, native_verified=found)
        else:
            self._update_if_launch(ws_id, agent_id, nonce, native_verified=found)

    def tick(self) -> None:
        with self._lock:
            pending = dict(self._pending)
        now = self._clock()
        for agent_id, info in pending.items():
            if now - info["started"] < READY_TIMEOUT:
                continue
            agent = self.store.get_agent(info["ws_id"], agent_id)
            if agent and agent["last_state"] == "starting":
                self._terminate_launch(info["ws_id"], agent, "no ready heartbeat within 60 s")
            with self._lock:
                self._pending.pop(agent_id, None)

    def _wrapper_pid(self, agent: dict) -> int | None:
        last_launch = agent.get("last_launch") or {}
        return last_launch.get("wrapper_pid") or last_launch.get("pid")

    def _terminate_launch(self, ws_id: str, agent: dict, reason: str) -> None:
        self._tmux.kill_session(self.tmux_name(agent))
        pid = self._wrapper_pid(agent)
        if pid:
            try:
                self._kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pid = None
        self.registry.deregister(agent["registry_name"])
        tail = self._log_tail(agent["agent_id"])
        self.store.update_agent(
            ws_id, agent["agent_id"], last_state="exited",
            last_error=f"{reason}\n{tail}".strip(),
        )
        if pid:
            def hard_kill():
                self._sleep(5)
                try:
                    self._kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

            self._background(hard_kill)

    def stop(self, ws_id: str, agent_id: str) -> dict:
        agent = self.store.get_agent(ws_id, agent_id)
        if agent is None:
            raise LaunchError(404, "agent not found")
        if agent["last_state"] in ("starting", "running"):
            self.checkpoint(ws_id)
            self._terminate_launch(ws_id, agent, "stopped by user")
            self.store.update_agent(ws_id, agent_id, last_error=None)
        return self.store.get_agent(ws_id, agent_id)

    def checkpoint(self, ws_id: str) -> dict:
        ws = self.store.get(ws_id)
        if ws is None:
            raise LaunchError(404, "session not found")
        checked = 0
        for agent in ws["agents"]:
            if agent["last_state"] != "running":
                continue
            checked += 1
            adapter = self._adapter(agent["provider"])
            if agent["native_session_id"] is None:
                try:
                    session_id = adapter.discover_session_id(self.launch_context_for(agent), 0)
                except AmbiguousSessionId as exc:
                    self.store.update_agent(ws_id, agent["agent_id"], history_note=str(exc))
                    session_id = None
                if session_id:
                    self.store.update_agent(
                        ws_id, agent["agent_id"], native_session_id=session_id
                    )
            self._verify_transcript(ws_id, agent["agent_id"], adapter)
            if not Path(agent["cwd"]).is_dir():
                self.store.update_agent(
                    ws_id, agent["agent_id"], last_error=f"cwd missing: {agent['cwd']}"
                )
        return {"checked": checked}

    def reconcile(self) -> None:
        for ws in self.store.list(include_archived=False):
            for agent in ws["agents"]:
                if agent["last_state"] not in ("starting", "running"):
                    continue
                if self._tmux.has_session(self.tmux_name(agent)):
                    if agent["last_state"] == "starting":
                        with self._lock:
                            self._pending[agent["agent_id"]] = {
                                "ws_id": ws["id"], "started": self._clock(),
                            }
                    elif agent["native_session_id"] is None:
                        self._background(
                            self._after_ready_discovery_only, ws["id"], agent["agent_id"],
                            (agent.get("last_launch") or {}).get("nonce"),
                        )
                else:
                    self.store.update_agent(ws["id"], agent["agent_id"], last_state="exited")

    def _after_ready_discovery_only(self, ws_id: str, agent_id: str, nonce: str) -> None:
        agent = self.store.get_agent(ws_id, agent_id)
        if not agent:
            return
        adapter = self._adapter(agent["provider"])
        try:
            session_id = adapter.discover_session_id(
                self.launch_context_for(agent), DISCOVERY_TIMEOUT
            )
        except AmbiguousSessionId as exc:
            self._update_if_launch(ws_id, agent_id, nonce, history_note=str(exc))
            return
        if session_id and self._update_if_launch(
            ws_id, agent_id, nonce, native_session_id=session_id
        ):
            self._verify_transcript(ws_id, agent_id, adapter, nonce)

    def unread_for(self, ws_id: str, agent_id: str) -> list[dict]:
        ws = self.store.get(ws_id)
        agent = self.store.get_agent(ws_id, agent_id) if ws else None
        if not agent:
            return []
        messages = self.messages.get_since(-1, channel=ws["channel"])
        return unread(agent, messages, self.store.routing_for(ws_id))

    def _send_bundle(self, ws: dict, agent: dict) -> int:
        items = self.unread_for(ws["id"], agent["agent_id"])
        if not items:
            return 0
        self.agents.trigger_sync(
            agent["registry_name"], message=f"{len(items)} unread", channel=ws["channel"],
            prompt=bundle_prompt(ws["channel"], items),
        )
        return len(items)

    def retry(self, ws_id: str, agent_id: str) -> None:
        ws = self.store.get(ws_id)
        agent = self.store.get_agent(ws_id, agent_id) if ws else None
        if agent is None:
            raise LaunchError(404, "agent not found")
        if not self.unread_for(ws_id, agent_id):
            raise LaunchError(400, f"nothing unread for {agent['registry_name']}")
        if agent["last_state"] != "running":
            raise LaunchError(409, f"{agent['registry_name']} is not running; resume it first")
        self._send_bundle(ws, agent)
