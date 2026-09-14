"""Workspace HTTP facade and user-facing session/agent resolution."""

from dataclasses import dataclass
import os
import shlex
import subprocess
import sys

import cli_api
from cli_api import CLIError
from cli_view_contracts import _safe
from urllib.parse import quote, urlencode


WINDOWS_TMUX_ERROR = "Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch."


@dataclass(frozen=True)
class WorkspaceCommandResult:
    """Original API response plus any workspace already fetched for dispatch."""

    data: object
    workspace: dict | None = None


@dataclass(frozen=True)
class AttachTarget:
    """Validated tmux target ready for foreground attachment."""

    target: str
    label: str
    hint: str
    nested: bool


def _session_choices(items):
    return ", ".join(f"{_safe(item.get('name', ''))} ({_safe(item.get('id', ''))})"
                     for item in items)


def resolve_session(items, selector):
    """Resolve an exact id, exact name, or unique name prefix."""
    by_id = [item for item in items if item.get("id") == selector]
    if len(by_id) == 1:
        return by_id[0]
    by_name = [item for item in items if item.get("name") == selector]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise CLIError(f"ambiguous session {_safe(selector)}: {_session_choices(by_name)}")
    by_prefix = [item for item in items
                 if str(item.get("name", "")).startswith(selector)]
    if len(by_prefix) == 1:
        return by_prefix[0]
    if len(by_prefix) > 1:
        raise CLIError(f"ambiguous session {_safe(selector)}: {_session_choices(by_prefix)}")
    raise CLIError(f"session not found: {_safe(selector)}")


def _agent_choices(items):
    return ", ".join(f"{_safe(item.get('registry_name', ''))} "
                     f"({_safe(item.get('agent_id', ''))})" for item in items)


def resolve_agent(workspace, selector):
    """Resolve an agent inside one workspace."""
    agents = workspace.get("agents", [])
    exact = [agent for agent in agents if selector in
             (agent.get("registry_name"), agent.get("agent_id"))]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise CLIError(f"ambiguous agent {_safe(selector)}: {_agent_choices(exact)}")
    by_provider = [agent for agent in agents if agent.get("provider") == selector]
    if len(by_provider) == 1:
        return by_provider[0]
    if len(by_provider) > 1:
        raise CLIError(f"ambiguous agent {_safe(selector)}: {_agent_choices(by_provider)}")
    raise CLIError(f"agent not found: {_safe(selector)}")


def require_tmux_platform():
    """Reject local terminal launches on Windows before calling their APIs."""
    if os.name == "nt":
        raise CLIError(WINDOWS_TMUX_ERROR)


def tmux_target(agent):
    """Use the server target, falling back only for older agent records."""
    if 'tmux_session' in agent:
        target = agent['tmux_session']
    else:
        agent_id = agent.get('agent_id')
        target = f'agentchattr-{agent_id}' if agent_id else None
    if not isinstance(target, str) or not target:
        raise CLIError('Agent terminal session is unavailable')
    return target


def prepare_attach(agent, *, runner=subprocess.run, shell_session=None) -> AttachTarget:
    """Validate and probe an existing tmux session without taking the terminal."""
    require_tmux_platform()
    if shell_session is not None and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CLIError('Attach requires a terminal')
    target = tmux_target(agent)
    label = shlex.quote(_safe(agent.get('registry_name') or agent.get('agent_id', '')))
    hint = f'/resume {label}' if shell_session is None else (
        f'python cli.py resume {label} --session {shlex.quote(_safe(shell_session))}')
    try:
        probe = runner(['tmux', 'has-session', '-t', '=' + target], timeout=5,
                       capture_output=True)
        if probe.returncode:
            raise CLIError(f'not running; resume with {hint}')
        nested = bool(os.environ.get('TMUX'))
    except (OSError, subprocess.SubprocessError):
        # Process diagnostics may contain credentials or terminal controls.
        raise CLIError('Could not attach to the agent terminal. Check tmux and retry.') from None
    return AttachTarget(target=target, label=label, hint=hint, nested=nested)


def run_attach(target, *, runner=subprocess.run, output=print) -> int:
    """Hand the terminal to a previously validated tmux target."""
    try:
        result = runner(['tmux', 'switch-client' if target.nested else 'attach',
                         '-t', target.target])
    except (OSError, subprocess.SubprocessError):
        # Process diagnostics may contain credentials or terminal controls.
        raise CLIError('Could not attach to the agent terminal. Check tmux and retry.') from None
    if target.nested and result.returncode == 0:
        output('Switch back: tmux switch-client -l')
    return result.returncode


def attach_agent(agent, *, runner=subprocess.run, output=print, shell_session=None):
    """Hand over the terminal to an existing tmux session; never launch one."""
    target = prepare_attach(agent, runner=runner, shell_session=shell_session)
    return run_attach(target, runner=runner, output=output)


def _selected_workspace(api, selector):
    if not selector:
        raise CLIError("a session selector is required")
    return api.resolve(selector, include_archived=True)


def run_workspace_command(api, args):
    """Dispatch one non-interactive workspace command through WorkspaceAPI."""
    command = args.command
    if command == "sessions":
        return WorkspaceCommandResult(api.list(bool(args.archived)))
    if command == "new":
        return WorkspaceCommandResult(api.create(args.session_name))

    if command in ("spawn", "resume"):
        require_tmux_platform()
    selector = args.target_session if command == "archive" else args.session
    workspace = _selected_workspace(api, selector)
    ws_id = workspace["id"]

    provider_options = {}
    if command in ('spawn', 'resume') and getattr(args, 'provider_flags', None) is not None:
        from provider_args import parse_provider_flags
        try:
            provider_options['provider_args'] = parse_provider_flags(args.provider_flags)
        except ValueError as error:
            raise CLIError(str(error)) from None
    if command == "spawn":
        data = api.action(ws_id, "spawn", body={
            "provider": args.provider,
            "cwd": args.cwd,
            "history_mode": args.history_mode,
            "name": args.agent_name,
            **provider_options,
        })
    elif command == "archive":
        data = api.action(ws_id, "archive")
    elif command == "unread":
        agent_id = None
        if args.agent:
            agent_id = resolve_agent(workspace, args.agent)["agent_id"]
        data = api.unread(ws_id, agent_id)
    else:
        selected = resolve_agent(workspace, args.agent)
        agent_id = selected["agent_id"]
        if command == "resume":
            data = api.action(ws_id, "resume", agent_id, body={
                "fresh": bool(args.fresh),
                "name": args.agent_name,
                "cwd": args.cwd,
                **provider_options,
            })
        elif command == "history":
            data = api.action(ws_id, "history", agent_id,
                              body={"mode": args.history_mode})
        elif command in ("stop", "retry"):
            data = api.action(ws_id, command, agent_id)
        else:
            raise CLIError(f"unsupported session command: {_safe(command)}")
    return WorkspaceCommandResult(data, workspace)


def _workspace_label(workspace):
    name = workspace.get("name") or workspace.get("id", "")
    ws_id = workspace.get("id", "")
    return f"{name} ({ws_id})"


def _agent_state(workspace, row):
    for item in (workspace or {}).get("agents", []):
        if row.get("agent_id") == item.get("agent_id"):
            return item.get("last_state", "unknown")
    return row.get("last_state", "unknown")


def format_workspace_result(command, result):
    """Return unsanitized human-readable text for one workspace result."""
    data = result.data
    if command == "sessions":
        lines = []
        for ws in data.get("workspaces", []):
            lines.append(_workspace_label(ws) + (' (archived)' if ws.get('archived') else ''))
            agents = ws.get("agents", [])
            if not agents:
                lines.append("  no agents")
            for item in agents:
                unread_count = item.get("unread_count", 0)
                unread_text = f" (unread {unread_count})" if unread_count else ""
                lines.append(f"  {item.get('registry_name', item.get('agent_id', ''))} "
                             f"{item.get('last_state', 'unknown')}{unread_text}")
        if data.get("warning"):
            lines.append(str(data["warning"]))
        return "\n".join(lines) if lines else "No sessions."
    if command == "new":
        return f"{_workspace_label(data)} #{data.get('channel', '')}"
    if command == "archive":
        return f"Archived {_workspace_label(data)}"
    if command == "unread":
        lines = []
        for row in data.get("agents", []):
            lines.append(f"{row.get('registry_name', row.get('agent_id', ''))} "
                         f"{_agent_state(result.workspace, row)} unread {row.get('count', 0)}")
            for message in row.get("messages", []):
                lines.append(f"  #{message.get('id', '')} {message.get('sender', '?')} "
                             f"{message.get('text', '')}")
        return "\n".join(lines) if lines else "No unread messages."
    if command == "retry":
        return "Retry requested."
    if command in ("spawn", "resume", "stop", "history"):
        return f"{data.get('registry_name', data.get('agent_id', ''))} " \
               f"{data.get('last_state', 'unknown')}"
    raise CLIError(f"unsupported session command: {_safe(command)}")


class WorkspaceAPI:
    def __init__(self, url, timeout=15):
        self.url = cli_api.local_url(url)
        self.timeout = timeout
        self._token = None

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - cli_api.monotonic()
        if remaining <= 0:
            raise CLIError("Request timed out")
        return remaining

    def request(self, method, path, body=None):
        deadline = cli_api.monotonic() + self.timeout
        if self._token is None:
            try:
                self._token = cli_api.fetch_session_token(
                    self.url, timeout=self._remaining(deadline))
            except CLIError:
                raise
            except OSError:
                raise CLIError("Could not connect to the local agentchattr server") from None
            except ValueError as error:
                raise CLIError(str(error)) from None
        try:
            return cli_api.request_json(
                self.url, self._token, method, path, body=body,
                timeout=self._remaining(deadline))
        except CLIError as error:
            if error.status in (401, 403):
                self._token = None
            raise

    def list(self, include_archived=False):
        query = urlencode({"include_archived": "1" if include_archived else "0"})
        return self.request("GET", "/api/workspaces?" + query)

    def get(self, ws_id):
        return self.request("GET", "/api/workspaces/" + quote(ws_id, safe=""))

    def settings(self):
        return self.request('GET', '/api/settings')

    def set_loop_guard(self, hops):
        return self.request('PATCH', '/api/settings/loop-guard', {'max_agent_hops': hops})

    def resolve(self, selector, include_archived=True):
        return resolve_session(self.list(include_archived)["workspaces"], selector)

    def create(self, name=""):
        return self.request("POST", "/api/workspaces", {"name": name})

    def rename(self, ws_id, name):
        root = "/api/workspaces/" + quote(ws_id, safe="")
        return self.request("PATCH", root, {"name": name})

    def action(self, ws_id, action, agent_id=None, body=None):
        root = "/api/workspaces/" + quote(ws_id, safe="")
        if action == "spawn":
            path = root + "/agents"
        elif agent_id is None:
            path = root + "/" + action
        else:
            path = root + "/agents/" + quote(agent_id, safe="") + "/" + action
        if action in ("resume", "history") and body is None:
            body = {}
        elif action in ("stop", "retry", "remove"):
            body = None
        return self.request("POST", path, body)

    def unread(self, ws_id, agent_id=None):
        path = "/api/workspaces/" + quote(ws_id, safe="") + "/unread"
        if agent_id is not None:
            path += "?" + urlencode({"agent_id": agent_id})
        return self.request("GET", path)

    def status(self):
        return self.request("GET", "/api/status")
