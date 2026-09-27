"""Server-owned process control for workspace agents (spec §2, §4, §6, §7)."""
from __future__ import annotations

import json
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

from agent_profiles import ORCHESTRATOR_INSTRUCTIONS, make_profile, profile_prompt
from providers import AmbiguousSessionId, LaunchContext, get_adapter
from provider_args import validate_provider_args
from registry import NameInUse
from workspace_store import HISTORY_MODES
from workspace_unread import bundle_prompt, unread
from workspace_processes import stop_wrapper_process

log = logging.getLogger(__name__)

READY_TIMEOUT = 60.0
DISCOVERY_TIMEOUT = 60.0
VERIFY_DELAY = 10.0
LOG_TAIL_LINES = 20
_ANY_LAUNCH = object()

LITERAL_PROMPT = ("Catch up: use mcp to read #{channel} with since_id=-1 and keep reading while "
                  "has_more is true, then respond in #{channel} with a two-line status of where "
                  "things stand.")


class LaunchError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class TmuxOps:
    def session_absent(self, name: str) -> bool:
        """Only recognized tmux absence proves that relaunch is safe."""
        try:
            result = subprocess.run(['tmux', 'has-session', '-t', '=' + name],
                                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        error = (result.stderr or '').lower()
        return result.returncode != 0 and any(text in error for text in (
            "can't find session", 'no server running', 'no such file or directory'))

    def remove_session(self, name: str) -> None:
        """Removal requires verified absence, not best-effort kill success."""
        target = '=' + name
        def present():
            result = subprocess.run(['tmux', 'has-session', '-t', target],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return True
            error = (result.stderr or '').lower()
            if any(text in error for text in ("can't find session", 'no server running', 'no such file or directory')):
                return False
            raise RuntimeError('Could not verify the agent tmux session; agent was kept for retry')
        if not present():
            return
        result = subprocess.run(['tmux', 'kill-session', '-t', target],
                                capture_output=True, text=True, timeout=5)
        if result.returncode or present():
            raise RuntimeError('Could not stop the agent tmux session; agent was kept for retry')

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def has_session(self, name: str) -> bool:
        try:
            return subprocess.run(
                ["tmux", "has-session", "-t", '=' + name], capture_output=True, timeout=5
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
        self._lifecycle_lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        self.on_startup_ready = None

    def tmux_name(self, agent: dict) -> str:
        return f"agentchattr-{agent['agent_id']}"

    def _adapter(self, provider: str):
        return self._adapters(provider, self.config.get("agents", {}).get(provider, {}))

    def _channel_latest_id(self, channel: str) -> int:
        recent = self.messages.get_recent(1, channel=channel)
        return recent[-1]["id"] if recent else -1

    def _floor_for(self, mode: str, channel: str) -> int:
        return 0 if mode == "literal" else self._channel_latest_id(channel) + 1

    def _validate(self, ws: dict | None, provider: str, cwd: str):
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
        try:
            return self._adapter(provider)
        except (ImportError, AttributeError, ValueError) as exc:
            raise LaunchError(400, f"adapter for {provider} could not be loaded: {exc}") from exc

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
        return cmd + ['--'] + list(provider_args) + agent.get('provider_args', [])

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
            self._processes[agent['agent_id']] = proc
        return proc.pid

    def _log_tail(self, agent_id: str) -> str:
        path = self.data_dir / "logs" / f"wrapper-{agent_id}.log"
        try:
            return "\n".join(path.read_text("utf-8", errors="replace").splitlines()[-LOG_TAIL_LINES:])
        except OSError:
            return ""

    def _register(self, ws: dict, provider: str, preferred: str, custom: bool,
                  allow_reserved: bool = False, spawning: bool = False) -> dict:
        try:
            return self.registry.register(
                provider, label=f"{ws['name']} {provider}", preferred_name=preferred,
                allow_reserved=allow_reserved,
            )
        except NameInUse as exc:
            if custom:
                raise LaunchError(400, f"name in use: {exc.name} ({exc.reason})") from exc
            action = "retry" if spawning else "stop that agent or resume with --agent-name <new>"
            raise LaunchError(
                409, f"name {exc.name} in use; {action} ({exc.reason})"
            ) from exc

    def spawn(self, ws_id: str, provider: str, cwd: str, history_mode: str,
              name: str | None = None, provider_args: list[str] | None = None,
              *, role="generalist", personality="pragmatic", _kind="worker") -> dict:
        with self._lifecycle_lock:
            return self._spawn(ws_id, provider, cwd, history_mode, name, provider_args,
                               role=role, personality=personality, _kind=_kind)

    def _spawn(self, ws_id: str, provider: str, cwd: str, history_mode: str,
               name: str | None = None, provider_args: list[str] | None = None,
              *, role="generalist", personality="pragmatic", _kind="worker") -> dict:
        try:
            profile = make_profile(role, personality)
            if _kind == "orchestrator":
                profile["role_instructions"] = ORCHESTRATOR_INSTRUCTIONS
            provider_args = validate_provider_args([] if provider_args is None else provider_args)
        except ValueError as error:
            raise LaunchError(400, str(error)) from None
        ws = self.store.get(ws_id)
        adapter = self._validate(ws, provider, cwd)
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
            reg = self._register(ws, provider, preferred, custom=bool(name), spawning=True)
            session_id = adapter.allocate_session_id()
            last_launch = {
                "kind": "spawn", "nonce": uuid.uuid4().hex, "at": _now_iso(), "pid": None,
                "identity_prompt_sent": False, "startup_delivery_done": False,
            }
            agent = self.store.add_agent(
                ws_id, provider=provider, cwd=str(Path(cwd).resolve()), history_mode=history_mode,
                registry_name=reg["name"], floor_id=self._floor_for(history_mode, ws["channel"]),
                native_session_id=session_id,
                history_state="done" if history_mode == "none" else "pending",
                last_launch=last_launch, provider_args=provider_args, profile=profile, kind=_kind,
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
                if _kind == "orchestrator":
                    self.store.update_agent(ws_id, agent["agent_id"], last_state="exited", last_error=str(exc))
                    self.store.set_orchestrator(ws_id, enabled=False, last_error=str(exc))
                else:
                    self.store.delete_identity(agent["agent_id"])
                    self.store.remove_agent(ws_id, agent["agent_id"])
            if isinstance(exc, LaunchError):
                raise
            raise LaunchError(500, f"failed to start wrapper: {exc}")
        last_launch["wrapper_pid"] = wrapper_pid
        return self.store.update_agent(ws_id, agent["agent_id"], last_launch=last_launch)

    def startup_ready(self, name: str) -> bool:
        found = self.store.find_agent_by_registry_name(name)
        if not found:
            return True
        ws, agent = found
        launch = agent.get("last_launch") or {}
        return (not ws.get("archived") and agent.get("last_state") == "running"
                and not launch.get("terminated")
                and launch.get("startup_delivery_done", not bool(agent.get("profile"))))

    def _manager_needs_fresh(self, agent: dict, cwd: str | None = None) -> bool:
        adapter = self._adapter(agent["provider"])
        session_id = agent.get("native_session_id")
        if not session_id or not adapter.supports_resume:
            return True
        return (adapter.can_locate_transcripts
                and adapter.locate_transcript(session_id, Path(cwd or agent["cwd"])) is None)

    def configure_orchestrator(self, ws_id: str, provider: str, cwd: str,
                               provider_args: list[str] | None = None) -> dict:
        with self._lifecycle_lock:
            ws = self.store.get(ws_id)
            self._validate(ws, provider, cwd)
            control = ws.get("orchestrator") or {}
            agent = self.store.get_agent(ws_id, control.get("agent_id"))
            if agent:
                if agent["provider"] != provider:
                    raise LaunchError(409, "Saved orchestrator provider is fixed; remove it before changing provider")
                if agent["last_state"] not in ("starting", "running"):
                    self.resume(ws_id, agent["agent_id"], fresh=self._manager_needs_fresh(agent, cwd),
                                cwd=cwd, provider_args=provider_args)
                self.store.set_orchestrator(ws_id, enabled=True, retry_count=0, next_retry_at=0)
            else:
                self._spawn(ws_id, provider, cwd, "none", provider_args=provider_args,
                            _kind="orchestrator")
            return self.store.get(ws_id)

    def resume(self, ws_id: str, agent_id: str, fresh: bool = False,
               name: str | None = None, cwd: str | None = None,
               provider_args: list[str] | None = None) -> dict:
        with self._lifecycle_lock:
            result = self._resume(ws_id, agent_id, fresh, name, cwd, provider_args)
            if result.get("kind") == "orchestrator":
                self.store.set_orchestrator(ws_id, enabled=True, retry_count=0, next_retry_at=0, last_error=None)
            return result

    def _resume(self, ws_id: str, agent_id: str, fresh: bool = False,
                name: str | None = None, cwd: str | None = None,
                provider_args: list[str] | None = None) -> dict:
        ws = self.store.get(ws_id)
        agent = self.store.get_agent(ws_id, agent_id) if ws else None
        if agent is None:
            raise LaunchError(404, "agent not found")
        try:
            provider_args = validate_provider_args(agent.get('provider_args', [])
                                                   if provider_args is None else provider_args)
        except ValueError as error:
            raise LaunchError(400, str(error)) from None
        requested_cwd = cwd if cwd is not None else agent["cwd"]
        adapter = self._validate(ws, agent["provider"], requested_cwd)
        effective_cwd = str(Path(requested_cwd).resolve())
        if agent["last_state"] in ("starting", "running") or self._tmux.has_session(self.tmux_name(agent)):
            raise LaunchError(409, f"{agent['registry_name']} is already running; "
                              "use Attach to open the existing terminal")
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
        restore['provider_args'] = agent.get('provider_args', [])
        preferred = name or agent["registry_name"]
        if name and name != agent["registry_name"] and name in set(self.store.member_names()):
            raise LaunchError(400, f"name in use by a saved agent: {name}")
        reg = None
        try:
            reg = self._register(
                ws, agent["provider"], preferred, custom=False,
                allow_reserved=(preferred == agent["registry_name"]),
            )
            fields = {"registry_name": reg["name"], "last_state": "starting", "last_error": None,
                      "provider_args": provider_args}
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
                "identity_prompt_sent": False, "startup_delivery_done": False,
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
        with self._lifecycle_lock:
            self._on_heartbeat(registry_name, ready, pid)

    def _on_heartbeat(self, registry_name: str, ready: bool, pid: int | None) -> None:
        found = self.store.find_agent_by_registry_name(registry_name)
        if not found:
            return
        ws, agent = found
        if not ready or ws.get("archived"):
            return
        if (agent["last_state"] == "running"
                and (agent.get("last_launch") or {}).get("startup_delivery_done") is False):
            self._background(self._after_ready, ws["id"], agent["agent_id"], agent["last_launch"].get("nonce"))
            return
        recovering = agent["last_state"] == "exited"
        if recovering:
            launch = agent.get('last_launch') or {}
            expected_pid = self._wrapper_pid(agent)
            # Crash-timeout deregistration can race a surviving wrapper's next
            # heartbeat. Restore only that recorded launch, never a new occupant.
            if (ws.get('archived') or launch.get('terminated')
                    or type(pid) is not int or pid <= 0
                    or type(expected_pid) is not int or expected_pid <= 0
                    or pid != expected_pid
                    or not self._tmux.has_session(self.tmux_name(agent))):
                return
        elif agent["last_state"] != "starting":
            return
        last_launch = dict(agent["last_launch"])
        last_launch["pid"] = pid
        self.store.update_agent(
            ws["id"], agent["agent_id"], last_state="running", last_error=None,
            last_launch=last_launch
        )
        with self._lock:
            self._pending.pop(agent["agent_id"], None)
        # Missing delivery flags belong to older launches; recovery must not
        # replay their identity/history prompts. Explicit pending delivery retries.
        if not recovering or last_launch.get('startup_delivery_done') is False:
            self._background(self._after_ready, ws["id"], agent["agent_id"], last_launch.get("nonce"))

    def _update_if_launch(self, ws_id: str, agent_id: str, nonce: str, **fields) -> bool:
        ok = self.store.update_agent_if_launch(ws_id, agent_id, nonce, **fields)
        if not ok:
            log.info("dropping stale background result for %s (launch changed)", agent_id)
        return ok

    def _after_ready(self, ws_id: str, agent_id: str, nonce: str) -> None:
        with self._lifecycle_lock:
            ws = self.store.get(ws_id)
            agent = self.store.get_agent(ws_id, agent_id)
            if not ws or not agent or (agent.get("last_launch") or {}).get("nonce") != nonce:
                return
            if (ws.get("archived") or agent['last_state'] != 'running' or agent['last_launch'].get('terminated')
                    or agent['last_launch'].get('startup_delivery_done')):
                return
            kind = agent["last_launch"].get("kind", "spawn")
            catch_up = agent["history_mode"] == "literal" and agent["history_state"] == "pending"
            identity_pending = not agent["last_launch"].get("identity_prompt_sent", False)
            if catch_up or identity_pending:
                if catch_up and not self._update_if_launch(ws_id, agent_id, nonce, history_state="done"):
                    return
                prompt = (
                    f"Your assigned agent name for this session is {json.dumps(agent['registry_name'])}. "
                    f"Your session channel is #{ws['channel']}. This assigned name is authoritative; "
                    "do not infer your identity from chat history or another agent's messages. "
                    "Use this exact name as sender when calling chat_send. "
                )
                prompt += profile_prompt(agent)
                prompt += (LITERAL_PROMPT.format(channel=ws['channel']) if catch_up else
                           "Wait for requests addressed to you in this session; no history catch-up is requested.")
                try:
                    self.agents.trigger_sync(
                        agent["registry_name"], message="catch up" if catch_up else "session identity",
                        channel=ws["channel"], prompt=prompt,
                        expected_token=(self.store.read_identity(agent_id) or {}).get('token'),
                    )
                except Exception:
                    if catch_up:
                        self._update_if_launch(ws_id, agent_id, nonce, history_state="pending")
                    log.exception("failed to enqueue startup prompt for %s", agent_id)
                    return
                launch = dict(agent["last_launch"], identity_prompt_sent=True)
                if not self._update_if_launch(ws_id, agent_id, nonce, last_launch=launch):
                    return
            if kind in ("resume", "fresh") or agent.get("profile"):
                try:
                    self._send_bundle(ws, self.store.get_agent(ws_id, agent_id))
                except Exception:
                    log.exception("failed to enqueue startup unread bundle for %s", agent_id)
                    return
            agent = self.store.get_agent(ws_id, agent_id)
            launch = dict(agent['last_launch'], startup_delivery_done=True)
            if not self._update_if_launch(ws_id, agent_id, nonce, last_launch=launch):
                return
            if self.on_startup_ready:
                try:
                    self.on_startup_ready(ws_id, agent_id)
                except Exception:
                    log.exception("startup-ready callback failed for %s", agent_id)
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
        with self._lifecycle_lock:
            self._tick()
            self._supervise_orchestrators()

    def _tick(self) -> None:
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

    def _supervise_orchestrators(self) -> None:
        for ws in self.store.list():
            control = ws.get("orchestrator") or {}
            if not control.get("enabled"):
                continue
            agent = self.store.get_agent(ws["id"], control.get("agent_id"))
            if not agent or agent.get("kind") != "orchestrator":
                continue
            if agent["last_state"] in ("starting", "running"):
                continue
            attempts = control.get("retry_count", 0)
            if attempts >= 3 or self._clock() < control.get("next_retry_at", 0):
                continue
            # Absence must be positively established. has_session() treats
            # command errors as absent, so it is unsuitable for supervision.
            probe = getattr(self._tmux, "session_absent", None)
            try:
                if probe is None or not probe(self.tmux_name(agent)):
                    continue
                process = self._processes.get(agent["agent_id"])
                if process is not None:
                    if process.poll() is None:
                        continue
                else:
                    pid = self._wrapper_pid(agent)
                    if type(pid) is int and pid > 1:
                        try:
                            self._kill(pid, 0)
                        except ProcessLookupError:
                            pass
                        else:
                            continue
            except (OSError, RuntimeError, subprocess.SubprocessError):
                continue
            self.store.set_orchestrator(ws["id"], retry_count=attempts + 1,
                                        next_retry_at=self._clock() + 5 * (2 ** attempts))
            try:
                self._resume(ws["id"], agent["agent_id"],
                             fresh=self._manager_needs_fresh(agent))
            except Exception as exc:
                self.store.set_orchestrator(ws["id"], last_error=str(exc))

    def _wrapper_pid(self, agent: dict) -> int | None:
        last_launch = agent.get("last_launch") or {}
        return last_launch.get("wrapper_pid") or last_launch.get("pid")

    def _terminate_launch(self, ws_id: str, agent: dict, reason: str) -> None:
        # Persist before best-effort process cleanup. A late heartbeat must not
        # undo a deliberate stop or timeout, even if killing tmux fails.
        self.store.update_agent(ws_id, agent['agent_id'],
                                last_launch=dict(agent.get('last_launch') or {}, terminated=True))
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

    def stop(self, ws_id: str, agent_id: str, *, expected_nonce=_ANY_LAUNCH) -> dict:
        with self._lifecycle_lock:
            return self._stop(ws_id, agent_id, expected_nonce=expected_nonce)

    def _stop(self, ws_id: str, agent_id: str, *, expected_nonce=_ANY_LAUNCH) -> dict:
        agent = self.store.get_agent(ws_id, agent_id)
        if agent is None:
            raise LaunchError(404, "agent not found")
        launch = agent.get('last_launch') or {}
        if expected_nonce is not _ANY_LAUNCH and expected_nonce != launch.get('nonce'):
            raise LaunchError(409, 'Agent launch changed; review Stop all agents again.')
        if agent.get("kind") == "orchestrator":
            self.store.set_orchestrator(ws_id, enabled=False)
        checkpoint_error = None
        try:
            # Fence late readiness before cleanup, even if a cleanup step fails.
            self.store.update_agent(ws_id, agent_id, last_launch=dict(launch, terminated=True))
            if agent['last_state'] in ('starting', 'running'):
                try:
                    self.checkpoint(ws_id, agent_id=agent_id)
                except Exception as error:
                    checkpoint_error = f'Agent stopped, but checkpoint failed: {error}'
            identity = self.store.read_identity(agent_id)
            with self._lock:
                process = self._processes.get(agent_id)
            pid = process.pid if process is not None else self._wrapper_pid(agent)
            stop_wrapper_process(pid, self.root,
                                 self.store.identity_path(agent_id), self.tmux_name(agent),
                                 owned_process=process)
            # Saved "exited" state cannot prove absence of leftover processes.
            self._tmux.remove_session(self.tmux_name(agent))
            token = (identity or {}).get('token')
            owned = self.registry.resolve_token(token) if token else None
            if owned and owned['name'] == agent['registry_name']:
                self.registry.deregister(owned['name'], expected_token=token, rename_remaining=False)
            with self._lock:
                self._pending.pop(agent_id, None)
                self._processes.pop(agent_id, None)
            self.store.update_agent(ws_id, agent_id, last_state='exited', last_error=checkpoint_error)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            message = f'Could not confirm agent stopped: {error}'
            self.store.update_agent(ws_id, agent_id, last_state='unknown', last_error=message)
            raise LaunchError(409, message) from error
        if checkpoint_error:
            raise LaunchError(409, checkpoint_error)
        return dict(self.store.get_agent(ws_id, agent_id), stop_confirmed=True)

    def remove(self, ws_id: str, agent_id: str) -> dict:
        with self._lifecycle_lock:
            agent = self.store.get_agent(ws_id, agent_id)
            if agent is None:
                raise LaunchError(404, 'agent not found')
            identity = self.store.read_identity(agent_id)
            name = agent['registry_name']
            try:
                if agent.get('kind') == 'orchestrator':
                    self.store.set_orchestrator(ws_id, enabled=False)
                self.store.update_agent(ws_id, agent_id, last_launch=dict(agent.get('last_launch') or {}, terminated=True))
                if agent['last_state'] == 'running':
                    self.checkpoint(ws_id)
                with self._lock:
                    proc = self._processes.get(agent_id)
                stop_wrapper_process(self._wrapper_pid(agent), self.root,
                                     self.store.identity_path(agent_id), self.tmux_name(agent),
                                     owned_process=proc)
                self._tmux.remove_session(self.tmux_name(agent))
                # A stopped saved name might now be used by an unrelated runtime
                # agent. Its token and queue must not be revoked by this removal.
                token = (identity or {}).get('token')
                owned = self.registry.resolve_token(token) if token else None
                if owned and owned['name'] == name:
                    self.registry.deregister(name, expected_token=token, rename_remaining=False)
                if self.registry.get_instance(name) is None:
                    (self.data_dir / f'{name}_queue.jsonl').unlink(missing_ok=True)
                    self.registry.clean_renames_for(name)
                self.store.identity_path(agent_id).unlink(missing_ok=True)
                with self._lock:
                    self._pending.pop(agent_id, None)
                    self._processes.pop(agent_id, None)
                self.store.remove_agent(ws_id, agent_id)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                raise LaunchError(409, f'Could not remove agent: {exc}') from exc
            return self.store.get(ws_id)

    def checkpoint(self, ws_id: str, *, agent_id: str | None = None) -> dict:
        ws = self.store.get(ws_id)
        if ws is None:
            raise LaunchError(404, "session not found")
        checked = 0
        for agent in ws["agents"]:
            if agent_id is not None and agent['agent_id'] != agent_id:
                continue
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

    def unread_for(self, ws_id: str, agent_id: str, *, routing: dict | None = None) -> list[dict]:
        ws = self.store.get(ws_id)
        agent = self.store.get_agent(ws_id, agent_id) if ws else None
        if not agent:
            return []
        since_id = min([agent["read_mark"]] + [mid - 1 for mid in agent.get("late_unacked_ids", [])])
        messages = self.messages.get_since(since_id, channel=ws["channel"])
        if routing is None:
            routing = self.store.routing_for(ws_id)
        return unread(agent, messages, routing)

    def _send_bundle(self, ws: dict, agent: dict) -> int:
        items = self.unread_for(ws["id"], agent["agent_id"])
        if not items:
            return 0
        self.agents.trigger_sync(
            agent["registry_name"], message=f"{len(items)} unread", channel=ws["channel"],
            prompt=bundle_prompt(ws["channel"], items),
            expected_token=(self.store.read_identity(agent['agent_id']) or {}).get('token'),
        )
        return len(items)

    def retry(self, ws_id: str, agent_id: str) -> None:
        with self._lifecycle_lock:
            self._retry(ws_id, agent_id)

    def _retry(self, ws_id: str, agent_id: str) -> None:
        ws = self.store.get(ws_id)
        agent = self.store.get_agent(ws_id, agent_id) if ws else None
        if agent is None:
            raise LaunchError(404, "agent not found")
        if not self.unread_for(ws_id, agent_id):
            raise LaunchError(400, f"nothing unread for {agent['registry_name']}")
        if agent["last_state"] != "running":
            raise LaunchError(409, f"{agent['registry_name']} is not running; resume it first")
        self._send_bundle(ws, agent)
