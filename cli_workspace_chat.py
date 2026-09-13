"""Interactive server startup and terminal session selection."""

import argparse
import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

import cli_api
from cli_api import CLIError
from cli_workspaces import (WINDOWS_TMUX_ERROR, WorkspaceCommandResult,
                            attach_agent, format_workspace_result, require_tmux_platform,
                            resolve_agent, resolve_session)
from cli_view_contracts import ActionOutcome, ViewEvent, _safe, terminal_text


ROOT = Path(__file__).resolve().parent
SERVER_SESSION = 'agentchattr-server'
_RESUME_COMMAND = ContextVar('resume_command', default=None)


def _resolved_path(value):
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve()


def _probe_status(url, timeout):
    """Return None only when transport cannot reach the endpoint."""
    deadline = time.monotonic() + timeout
    try:
        token = cli_api.fetch_session_token(url, timeout=timeout)
    except HTTPError as error:
        raise CLIError(f'Local server bootstrap failed (HTTP {error.code})', error.code) from None
    except OSError:
        return None
    except ValueError as error:
        raise CLIError(_safe(error)) from None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CLIError('Local server bootstrap exceeded the status probe deadline')
    try:
        status = cli_api.request_json(url, token, 'GET', '/api/status', timeout=remaining)
    except CLIError as error:
        if error.status is None and str(error) == 'Could not connect to the local agentchattr server':
            return None
        raise
    if (not isinstance(status, dict) or not isinstance(status.get('paused'), bool)
            or not isinstance(status.get('data_dir'), str) or not status['data_dir']):
        raise CLIError('The local endpoint did not return an agentchattr server status')
    return status


def ensure_server(url, *, explicit_url, config, output=print):
    """Check authenticated readiness, starting one local tmux server if down."""
    url = cli_api.local_url(url)
    data_dir = _resolved_path(config.get('server', {}).get('data_dir', './data'))
    log_path = data_dir / 'logs/server.log'
    hint = f'Start it manually: python run.py\nServer log: {_safe(log_path)}'
    session_hint = f'\nTmux session: {SERVER_SESSION}'

    def readiness_failure_hint():
        try:
            existing = subprocess.run(['tmux', 'has-session', '-t', '=' + SERVER_SESSION],
                                      capture_output=True, timeout=5)
            if existing.returncode == 0:
                return hint + session_hint
        except (OSError, subprocess.SubprocessError):
            pass
        return hint

    def ready(status):
        if _resolved_path(status['data_dir']) != data_dir:
            output(f'Warning: server data_dir {_safe(status["data_dir"])} '
                   f'differs from configured {_safe(data_dir)}')
        return status

    status = _probe_status(url, 2)
    if status is not None:
        return ready(status)
    if explicit_url:
        raise CLIError('The local server is unavailable.\n' + hint)
    if sys.platform == 'win32':
        raise CLIError(WINDOWS_TMUX_ERROR)
    if not shutil.which('tmux'):
        raise CLIError('tmux was not found.\n' + hint)
    try:
        existing = subprocess.run(['tmux', 'has-session', '-t', '=' + SERVER_SESSION],
                                  capture_output=True, timeout=5)
        if existing.returncode == 0:
            raise CLIError(f'Tmux session {SERVER_SESSION} already exists but the server is unavailable.\n'
                           + hint + session_hint)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(ROOT / 'run.py'),
                '--port', str(config.get('server', {}).get('port', 8300)),
                '--data-dir', str(data_dir),
                '--upload-dir', str(_resolved_path(config.get('images', {}).get('upload_dir', './uploads'))),
                '--mcp-http-port', str(config.get('mcp', {}).get('http_port', 8200)),
                '--mcp-sse-port', str(config.get('mcp', {}).get('sse_port', 8201))]
        command = shlex.join(argv) + ' >> ' + shlex.quote(str(log_path)) + ' 2>&1'
        launched = subprocess.run(['tmux', 'new-session', '-d', '-s', SERVER_SESSION,
                                   '-c', str(ROOT), command], capture_output=True, timeout=5)
        if launched.returncode:
            raise CLIError('Could not start the local server.\n' + hint)
    except (OSError, subprocess.SubprocessError):
        raise CLIError('Could not start the local server.\n' + hint) from None
    deadline = time.monotonic() + 15
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            status = _probe_status(url, min(2, remaining))
        except CLIError as error:
            raise CLIError(_safe(error) + '\n' + readiness_failure_hint()) from None
        if status is not None:
            output(f'Started server in tmux session {SERVER_SESSION}.')
            return ready(status)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.25, remaining))
    raise CLIError('The local server did not become ready within 15 seconds.\n' + readiness_failure_hint())


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def _relative_age(value):
    if not value or not (stamp := _timestamp(value)):
        return 'age unknown'
    seconds = max(0, time.time() - stamp)
    for size, label in [(86400, 'd'), (3600, 'h'), (60, 'm')]:
        if seconds >= size:
            return f'{int(seconds // size)}{label} ago'
    return 'just now'


def _agent_label(agent):
    return agent.get('registry_name') or agent.get('agent_id', '')


def _agent_cwd(agent):
    cwd = agent.get('cwd')
    if not cwd:
        return None
    try:
        path = Path(cwd).resolve()
        return path if path.is_dir() else None
    except (OSError, ValueError):
        return None


def _agent_line(agent):
    state = agent.get('last_state', 'unknown')
    if state == 'starting' and (agent.get('last_launch') or {}).get('kind') == 'fresh':
        state = 'fresh'
    line = f'{_agent_label(agent)} {state}'
    if agent.get('unread_count'):
        line += f' (unread {agent["unread_count"]})'
    if not agent.get('native_session_id'):
        line += ' · id unknown'
    if agent.get('history_mode') == 'literal' and agent.get('history_state') == 'pending':
        line += ' · catching up…'
    if _agent_cwd(agent) is None:
        line += f' · ⚠ cwd missing — /resume {shlex.quote(str(_agent_label(agent)))} --cwd PATH'
    return _safe(line)


def _render_picker(items, output):
    output('Sessions')
    counts = Counter(ws.get('name') or ws['id'] for ws in items)
    for index, ws in enumerate(items, 1):
        name = ws.get('name') or ws['id']
        if counts[name] > 1:
            name += f' ({ws["id"].removeprefix("ws_")[:4]})'
        if ws.get('archived'):
            name += ' (archived)'
        agents = ' · '.join(_agent_line(agent) for agent in ws.get('agents', [])) or 'no agents'
        output(f'  {index}. {_safe(name)}     {agents}     {_relative_age(ws.get("updated_at"))}')
    output('  n. New session')
    output('  a. Show archived')


def _legacy_confirm(prompt):
    async def confirm(text, *, default=False, escape=False):
        answer = (await prompt(text, default='y' if default else 'n')).strip().lower()
        return answer in ('y', 'yes') or (default and answer == '')
    return confirm


async def _unarchive_workspace(api, workspace, *, confirm, mutate, still_current):
    """Return a workspace dict, or a cancelled ActionOutcome after decline/overlap."""
    if not workspace.get('archived'):
        return workspace
    ws_id = workspace['id']
    accepted = await confirm('Unarchive it? [y/N]', default=False, escape=False)
    if not still_current():
        return ActionOutcome('cancelled', workspace_id=ws_id)
    if not accepted:
        return ActionOutcome('cancelled', 'Archived session was not selected', ws_id)
    result = await mutate(api.action, ws_id, 'unarchive')
    if not still_current():
        return ActionOutcome('cancelled', workspace_id=ws_id)
    return result


async def _resume_workspace(api, workspace, *, confirm, notice, mutate, still_current, no_resume):
    """Return a workspace dict, or a cancelled ActionOutcome after overlap."""
    eligible = [agent for agent in workspace.get('agents', [])
                if agent.get('last_state') == 'exited' and _agent_cwd(agent) is not None]
    if no_resume or not eligible:
        return workspace
    if sys.platform == 'win32':
        notice(WINDOWS_TMUX_ERROR)
        return workspace
    ws_id = workspace['id']
    cancelled = ActionOutcome('cancelled', workspace_id=ws_id)
    accepted = await confirm(f'Resume {len(eligible)} stopped agents? [Y/n]', default=True, escape=False)
    if not still_current():
        return cancelled
    if not accepted:
        return workspace
    require_tmux_platform()
    resumed = False
    for agent in eligible:
        if not still_current():
            return cancelled
        try:
            result = await mutate(api.action, ws_id, 'resume', agent['agent_id'], body={})
            if isinstance(result, ActionOutcome):
                return result
            resumed = True
        except CLIError as error:
            notice(str(error))
            if error.status == 409 and '--fresh' in str(error):
                notice(f'/resume {shlex.quote(str(_agent_label(agent)))} --fresh')
        if not still_current():
            return cancelled
    if resumed:
        workspace = await asyncio.to_thread(api.get, ws_id)
        if not still_current():
            return cancelled
    return workspace


async def _offer_resume(api, workspace, prompt, output, no_resume):
    for agent in workspace.get('agents', []):
        output(_agent_line(agent))
    return await _resume_workspace(
        api, workspace, confirm=_legacy_confirm(prompt),
        notice=lambda text: output(_safe(text)),
        mutate=asyncio.to_thread, still_current=lambda: True, no_resume=no_resume)


async def choose_workspace(api, prompt, output, *, selector=None, no_resume=False):
    """Pick or create a workspace using authenticated API calls off-loop."""
    include_archived = selector is not None
    try:
        while True:
            data = await asyncio.to_thread(api.list, include_archived)
            if data.get('warning'):
                output(_safe(data['warning']))
            items = sorted(data['workspaces'], key=lambda ws: _timestamp(ws.get('updated_at')), reverse=True)
            if selector is not None:
                selected = resolve_session(items, selector)
            else:
                if items:
                    _render_picker(items, output)
                    answer = (await prompt('Choose:', default='')).strip().lower()
                    if answer == 'a':
                        include_archived = not include_archived
                        continue
                else:
                    answer = 'n'
                if answer == 'n':
                    name = (await prompt('Session name:', default='')).strip()
                    return await asyncio.to_thread(api.create, name)
                if not answer.isdecimal() or not 1 <= int(answer) <= len(items):
                    output('Choose a session number, n, or a.')
                    continue
                selected = items[int(answer) - 1]
            selected = await asyncio.to_thread(api.get, selected['id'])
            if selected.get('archived'):
                output('archived')
                selected = await _unarchive_workspace(
                    api, selected, confirm=_legacy_confirm(prompt),
                    mutate=asyncio.to_thread, still_current=lambda: True)
                if isinstance(selected, ActionOutcome):
                    if selector is not None:
                        raise CLIError(selected.message)
                    continue
            return await _offer_resume(api, selected, prompt, output, no_resume)
    except (EOFError, KeyboardInterrupt):
        return None


SESSION_COMMANDS = {'/spawn', '/resume', '/stop', '/retry', '/unread', '/history',
                    '/rename', '/archive', '/sessions', '/attach'}
SUMMARY_ERROR = 'summary history mode is not available in this version; use literal or none'
SESSION_HELP = """/spawn PROVIDER [--agent-name NAME] [--cwd PATH] [--history-mode none|literal]
/resume AGENT [--fresh] [--agent-name NAME] [--cwd PATH]
/attach AGENT        Attach to an agent terminal
/stop AGENT          Stop a session agent
/agents              Show session agents and other live agents
/unread [AGENT]      Show unread messages
/retry AGENT         Retry unread delivery
/history AGENT MODE  Change history mode: none or literal
/history             Show recent channel messages
/rename NAME         Rename this session
/archive             Archive this session, then choose another
/sessions            Checkpoint, then choose a session
/channels            List channels
/jobs                List jobs
/rules               List rules
/help                Show commands
/quit                Checkpoint and disconnect; agents keep running"""


class _ChatParser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError(message)


def _parse_command(command, words):
    parser = _ChatParser(prog=command, add_help=False, allow_abbrev=False)
    if command == '/spawn':
        parser.add_argument('provider')
        parser.add_argument('--cwd')
        parser.add_argument('--agent-name')
        parser.add_argument('--history-mode')
    elif command == '/resume':
        parser.add_argument('agent')
        parser.add_argument('--fresh', action='store_true')
        parser.add_argument('--agent-name')
        parser.add_argument('--cwd')
    elif command in ('/stop', '/retry', '/history', '/attach'):
        parser.add_argument('agent')
        if command == '/history':
            parser.add_argument('history_mode')
    elif command == '/unread':
        parser.add_argument('agent', nargs='?')
    elif command == '/rename':
        parser.add_argument('name')
    return parser.parse_args(words)


def _validate_history(mode):
    if mode == 'summary':
        raise CLIError(SUMMARY_ERROR)
    if mode not in ('none', 'literal'):
        raise CLIError('history mode must be literal or none')


class WorkspaceChatController:
    def __init__(self, client, api, *, selector=None, no_resume=False, plain_channel=False,
                 data_dir=None, providers=()):
        self.client = client
        self.api = api
        self.selector = selector
        self.no_resume = no_resume
        self.plain_channel = plain_channel
        self.workspace = None
        self.prompt = None
        self.data_dir = data_dir
        self.providers = list(providers)
        self._agent_states = {}
        self._failed_launches = set()
        self._refresh_requested = False
        self._closed = False
        self._selection_version = 0
        self._state_revision = 0
        self._poll_error = None
        self.presentation = None
        self.on_view_change = None
        self._action_lock = asyncio.Lock()
        self._active_actions = set()
        self._pending_actions = set()
        self._pending_mutations = set()
        self._selection_task = None
        self._selection_commit = None

    def bind_view(self, presentation, notify):
        self.presentation = presentation
        self.on_view_change = notify
        self.client.on_workspace = self.on_workspace
        self.client.on_settings = self.on_settings

    def _notify_view(self, kind, *, agent_id=None, text=None):
        if self.on_view_change is not None:
            workspace_id = self.workspace.get('id') if self.workspace is not None else None
            self.on_view_change(ViewEvent(
                'controller', kind, self._state_revision,
                workspace_id=workspace_id, agent_id=agent_id,
                selection_generation=self._selection_version, text=text))

    async def initialize(self, prompt):
        self.prompt = prompt
        if self.plain_channel:
            return True
        self._select(await choose_workspace(
            self.api, prompt, self.client.show, selector=self.selector, no_resume=self.no_resume))
        if self.workspace is None:
            return False
        self.client.channel = self.workspace['channel']
        self.client.on_workspace = self.on_workspace
        self.client.on_settings = self.on_settings
        if self.data_dir is None:
            try:
                self.data_dir = (await asyncio.to_thread(self.api.status)).get('data_dir')
            except Exception:
                self.client.show('Could not read server status; wrapper log directory is unknown.')
        self._agent_states = {a['agent_id']: self._agent_status(a) for a in self.workspace['agents']}
        return True

    def _select(self, workspace, *, closed=False):
        self.workspace = workspace
        self._selection_version += 1
        self._closed = closed
        self._failed_launches.clear()
        self._refresh_requested = workspace is not None
        self._poll_error = None
        self._agent_states = {a['agent_id']: self._agent_status(a)
                              for a in (workspace or {}).get('agents', [])}
        if workspace is not None:
            self.client.channel = workspace['channel']
            self.client.pending_channel = None
        self._notify_view('selection')

    @property
    def selection_pending(self):
        return self._selection_task is not None or self._selection_commit is not None

    async def list_sessions(self, include_archived=False):
        """Return the API mapping (workspaces and optional warning) on demand."""
        return await asyncio.to_thread(self.api.list, include_archived=include_archived)

    async def select_session(self, ws_id):
        """Prepare without changing selection; own the checkpoint/commit once ready."""
        try:
            self._validate_action('select_session', {'session_id': ws_id})
            if self.presentation is None:
                raise CLIError('Session selection requires a bound presenter.')
        except CLIError as error:
            return self._failed_action(error)
        if self.selection_pending:
            return ActionOutcome('cancelled', 'Session selection already in progress.', ws_id)
        if self.workspace is not None and self.workspace['id'] == ws_id:
            return ActionOutcome('completed', workspace_id=ws_id)
        generation = self._selection_version
        task = asyncio.create_task(self._prepare_selection(ws_id, generation))
        self._selection_task = task
        commit = None
        try:
            candidate = await task
            if isinstance(candidate, ActionOutcome):
                return candidate
            if self._selection_task is not task or generation != self._selection_version:
                return ActionOutcome('cancelled', workspace_id=ws_id)
            commit = asyncio.create_task(self._commit_selection(candidate, generation))
            self._selection_commit = commit
            self._pending_actions.add(commit)
            return await self._wait_owned(commit)
        except asyncio.CancelledError:
            if commit is not None or asyncio.current_task().cancelling():
                raise
            return ActionOutcome('cancelled', workspace_id=ws_id)
        except (CLIError, OSError, TimeoutError) as error:
            return self._failed_action(error, ws_id)
        finally:
            if self._selection_task is task:
                self._selection_task = None
            if self._selection_commit is commit:
                self._selection_commit = None
            if commit is not None:
                self._pending_actions.discard(commit)

    async def _prepare_selection(self, ws_id, generation):
        cancelled = ActionOutcome('cancelled', workspace_id=ws_id)

        def still_current():
            return generation == self._selection_version

        async def mutate(function, *args, **kwargs):
            async with self._action_lock:
                if not still_current():
                    return cancelled
                return await self._run_mutation(function, *args, **kwargs)

        candidate = await asyncio.to_thread(self.api.get, ws_id)
        if not still_current():
            return cancelled
        candidate = await _unarchive_workspace(
            self.api, candidate, confirm=self.presentation.confirm,
            mutate=mutate, still_current=still_current)
        if isinstance(candidate, ActionOutcome):
            return candidate
        return await _resume_workspace(
            self.api, candidate, confirm=self.presentation.confirm, notice=self._notice,
            mutate=mutate, still_current=still_current, no_resume=self.no_resume)

    async def _commit_selection(self, candidate, generation):
        async with self._action_lock:
            if generation != self._selection_version:
                return ActionOutcome('cancelled', workspace_id=candidate['id'])
            await self.close()
            if generation != self._selection_version:
                return ActionOutcome('cancelled', workspace_id=candidate['id'])
            # No await between clearing preparation state and the final event.
            self._selection_task = None
            self._selection_commit = None
            self._select(candidate)
            return ActionOutcome('completed', workspace_id=candidate['id'])

    async def cancel_selection(self):
        """Quit cancels preparation but drains an already-owned atomic commit."""
        task, commit = self._selection_task, self._selection_commit
        if commit is not None:
            await self._wait_owned(commit)
        elif task is not None:
            # Invalidate even a finished read whose caller has not resumed yet.
            self._selection_task = None
            task.cancel()
        if task is not None and task is not asyncio.current_task():
            waiter = asyncio.ensure_future(asyncio.gather(task, return_exceptions=True))
            await self._wait_owned(waiter)

    async def close(self):
        if self.workspace is None or self._closed:
            return
        self._closed = True
        try:
            await asyncio.to_thread(self.api.action, self.workspace['id'], 'checkpoint')
        except CLIError as error:
            self.client.show(f'Warning: checkpoint failed: {error}')
        except Exception:
            self.client.show('Warning: checkpoint failed or timed out. Disconnecting is still safe.')

    async def _pick_again(self):
        self._select(None)
        self.client.pause_output()
        try:
            self._select(await choose_workspace(
                self.api, self.prompt, lambda text: self.client.show(text, immediate=True),
                no_resume=self.no_resume))
        finally:
            self.client.resume_output()
        if self.workspace is None:
            return 'quit'
        self.client.history()
        return 'continue'

    def _snapshot_version(self):
        return self._selection_version, self._state_revision

    def _accept_snapshot(self, version):
        if self._closed or self.workspace is None or version[0] != self._selection_version:
            return False
        if version[1] != self._state_revision:
            # Events and HTTP responses are independent snapshots. A fresh read
            # reconciles an overlap without repeating a completed mutation.
            self._refresh_requested = True
            return False
        return True

    async def poll_forever(self):
        while True:
            await asyncio.sleep(2)
            workspace = self.workspace
            if workspace is None or self._closed:
                continue
            needs_refresh = (self._refresh_requested or self.client.websocket is None
                             or any(a.get('last_state') == 'starting'
                                    or a.get('history_state') == 'pending'
                                    for a in workspace.get('agents', [])))
            if not needs_refresh:
                continue
            version = self._snapshot_version()
            self._refresh_requested = False
            try:
                data = await asyncio.to_thread(self.api.get, workspace['id'])
            except Exception:
                if version[0] == self._selection_version:
                    message = 'Session refresh failed or timed out. Check the local server.'
                    if message != self._poll_error:
                        self.client.show(message)
                    self._poll_error = message
                    self._refresh_requested = True
                continue
            if self._accept_snapshot(version):
                self._poll_error = None
                self.on_workspace(data)

    def completion_words(self):
        if self.plain_channel or self.workspace is None:
            return []
        words = set(SESSION_COMMANDS) | set(self.providers)
        for agent in self.workspace['agents']:
            words.update(str(agent[key]) for key in ('agent_id', 'registry_name', 'provider') if agent.get(key))
        return sorted(words)

    def _agent_status(self, agent):
        line = _agent_line(agent) + f' · {agent.get("provider", "unknown")}'
        if _agent_cwd(agent) is not None:
            line += f' · cwd {agent["cwd"]}'
        if not agent.get('unread_count'):
            line += ' · unread 0'
        if agent.get('native_session_id'):
            line += ' · id present'
        if agent.get('history_state') in ('done', 'failed'):
            line += f' · history {agent["history_state"]}'
        for key in ('history_note', 'last_error'):
            if agent.get(key):
                line += f' · {agent[key]}'
        if agent['agent_id'] in self._failed_launches:
            line += ' · failed to start'
            if self.data_dir:
                line += f'; see {Path(self.data_dir) / "logs" / ("wrapper-" + agent["agent_id"] + ".log")}'
        return line

    def on_workspace(self, data):
        if self.workspace is None or data.get('id') != self.workspace['id']:
            return
        previous = {a['agent_id']: a for a in self.workspace['agents']}
        self.workspace = data
        self._state_revision += 1
        states = {}
        for agent in data.get('agents', []):
            agent_id = agent['agent_id']
            if agent.get('last_state') == 'starting':
                self._failed_launches.discard(agent_id)
            elif (agent.get('last_state') == 'exited' and agent.get('last_error')
                  and previous.get(agent_id, {}).get('last_state') == 'starting'):
                self._failed_launches.add(agent_id)
            elif agent.get('last_state') == 'running':
                self._failed_launches.discard(agent_id)
            line = self._agent_status(agent)
            states[agent_id] = line
            if self.on_view_change is None and self._agent_states.get(agent_id) != line:
                self.client.show(line)
        self._agent_states = states
        self._notify_view('agent_state')

    def on_settings(self, data):
        if self.workspace is not None:
            self.client.channel = self.workspace['channel']
            self.client.pending_channel = None
            self._refresh_requested = True

    def _show_agents(self):
        names = set()
        for agent in self.workspace['agents']:
            names.add(agent.get('registry_name'))
            self.client.show(self._agent_status(agent))
        for name, info in self.client.status.items():
            if name not in names and isinstance(info, dict):
                state = 'busy' if info.get('busy') else 'online' if info.get('available') else 'offline'
                self.client.show(f'@{name}: {state}' + (f' ({info["role"]})' if info.get('role') else ''))
        if self.client.status.get('paused'):
            self.client.show('An agent conversation is paused by the loop guard.')

    def _store_agent(self, agent):
        items = list(self.workspace['agents'])
        for index, current in enumerate(items):
            if current['agent_id'] == agent['agent_id']:
                items[index] = agent
                break
        else:
            items.append(agent)
        self.on_workspace(dict(self.workspace, agents=items))

    def _resume_hint(self, args, error):
        message = str(error).lower()
        fresh = error.status == 409 and '--fresh' in message
        cwd = error.status in (400, 409) and ('cwd' in message or '--cwd' in message)
        name = error.status in (400, 409) and 'name' in message and 'in use' in message
        if not (fresh or cwd or name):
            return
        words = ['/resume', args.agent]
        if fresh or args.fresh:
            words.append('--fresh')
        if name or args.agent_name:
            words.extend(['--agent-name', 'NAME' if name else args.agent_name])
        if cwd or args.cwd:
            words.extend(['--cwd', 'PATH' if cwd else args.cwd])
        self._notice(shlex.join(words))

    def _notice(self, text):
        if self.presentation is not None:
            self.presentation.notice('\n'.join(_safe(line.expandtabs(4)) for line in str(text).split('\n')))
        else:
            self.client.show(text)

    def _failed_action(self, error, ws_id=None, agent_id=None, *, resume=None):
        message = (terminal_text(error)
                   if isinstance(error, CLIError) else
                   'Session request failed or timed out. Check the local server and retry.')
        self._notice(message)
        if resume is not None and isinstance(error, CLIError):
            self._resume_hint(resume, error)
        return ActionOutcome('failed', message, ws_id, agent_id)

    @staticmethod
    async def _wait_owned(task):
        """Cancellation waits for ownership to finish, including repeated signals."""
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                # Retrieve the original exception below; never relabel local bugs.
                break
        try:
            return task.result()
        finally:
            if cancelled:
                raise asyncio.CancelledError

    async def _run_mutation(self, function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._pending_mutations.add(task)
        try:
            return await self._wait_owned(task)
        finally:
            self._pending_mutations.discard(task)

    async def wait_pending(self):
        """Drain owned operations/workers without replay; cancellation waits for them.

        Cancel queued/uncommitted action callers first: they can extend this drain.
        A presenter's attach must never re-enter execute_action or wait_pending.
        The action lock is not reentrant, and attach itself is owned pending work.
        """
        while self._pending_actions or self._pending_mutations:
            tasks = self._pending_actions | self._pending_mutations
            # return_exceptions consumes completed failures even during shutdown.
            waiter = asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))
            await self._wait_owned(waiter)

    def _validate_action(self, action, payload):
        fields = {
            'create_session': {'name'}, 'rename_session': {'name'},
            'select_session': {'session_id'},
            'archive_session': {'confirmed'},
            'spawn': {'provider', 'cwd', 'name', 'history_mode'},
            'resume': {'agent_id', 'fresh', 'cwd', 'name'},
            'stop': {'agent_id'}, 'attach': {'agent_id'}, 'unread': {'agent_id'},
            'retry': {'agent_id'}, 'history': {'agent_id', 'mode'},
        }
        if not isinstance(action, str) or action not in fields:
            raise CLIError(f'unsupported session action: {_safe(action)}')
        if not isinstance(payload, dict):
            raise CLIError('action payload must be an object')
        allowed = fields[action]
        required = set() if action == 'unread' else allowed
        if set(payload) - allowed:
            raise CLIError('unexpected action fields: ' + ', '.join(sorted(map(str, set(payload) - allowed))))
        if required - set(payload):
            raise CLIError('missing action fields: ' + ', '.join(sorted(required - set(payload))))
        for key, value in payload.items():
            if key in ('fresh', 'confirmed'):
                if not isinstance(value, bool):
                    raise CLIError(f'{key} must be a boolean')
            elif value is None and (key == 'cwd' and action == 'resume'
                                    or key == 'name' and action in ('spawn', 'resume')
                                    or key == 'agent_id' and action == 'unread'):
                continue
            elif not isinstance(value, str):
                raise CLIError(f'{key} must be text')
            elif (key in ('provider', 'agent_id', 'session_id') or key == 'cwd' and action == 'spawn') and not value:
                raise CLIError(f'{key} is required')
        mode = payload.get('history_mode', payload.get('mode'))
        if mode is not None:
            _validate_history(mode)
        if action in ('spawn', 'resume', 'attach'):
            require_tmux_platform()
        if self.plain_channel or (self.workspace is None and action not in ('create_session', 'select_session')):
            raise CLIError('This command requires a selected session; start chat with --session or the session picker.')
        agent_id = payload.get('agent_id')
        if agent_id is not None:
            # Structured payloads contain stable IDs only; slash adapters resolve aliases.
            if not any(agent.get('agent_id') == agent_id for agent in self.workspace['agents']):
                raise CLIError(f'agent not found: {_safe(agent_id)}')

    async def execute_action(self, action, payload):
        """Validate and serialize one lifecycle operation, never replaying a mutation."""
        ws_id = self.workspace['id'] if self.workspace is not None else None
        agent_id = payload.get('agent_id') if isinstance(payload, dict) else None
        if not isinstance(agent_id, str):
            agent_id = None
        try:
            self._validate_action(action, payload)
            payload = dict(payload)
            if action == 'unread':
                payload.setdefault('agent_id', None)
            agent_id = payload.get('agent_id')
        except CLIError as error:
            return self._failed_action(error, ws_id, agent_id)
        if action == 'select_session':
            return await self.select_session(payload['session_id'])
        generation = self._selection_version
        key = (ws_id, generation, action, tuple(sorted(payload.items())))
        if key in self._active_actions:
            message = 'Session action already in progress.'
            self._notice(message)
            return ActionOutcome('cancelled', message, ws_id, agent_id)
        self._active_actions.add(key)
        try:
            # Admission precedes the first await. Dialogs are gathered by callers.
            async with self._action_lock:
                if generation != self._selection_version or (self._closed and action != 'create_session'):
                    return ActionOutcome('cancelled', workspace_id=ws_id, agent_id=agent_id)
                try:
                    self._validate_action(action, payload)
                except CLIError as error:
                    return self._failed_action(error, ws_id, agent_id)
                task = asyncio.create_task(self._execute_locked(
                    action, payload, ws_id, self.workspace, self._snapshot_version()))
                self._pending_actions.add(task)
                try:
                    return await self._wait_owned(task)
                finally:
                    self._pending_actions.discard(task)
        finally:
            self._active_actions.discard(key)

    async def _execute_locked(self, action, payload, ws_id, workspace, version):
        agent_id = payload.get('agent_id')
        try:
            if action == 'create_session':
                result = await self._run_mutation(self.api.create, payload['name'])
                return ActionOutcome('completed', workspace_id=result['id'])
            if action == 'archive_session':
                if not payload['confirmed']:
                    return ActionOutcome('cancelled', workspace_id=ws_id)
                await self._run_mutation(self.api.action, ws_id, 'archive')
                if self.presentation is not None and version[0] == self._selection_version:
                    self._select(None, closed=True)
            elif action == 'attach':
                agent = next(a for a in workspace['agents'] if a['agent_id'] == agent_id)
                if self.presentation is not None:
                    return await self.presentation.attach(agent)
                self.client.pause_output()
                try:
                    loop = asyncio.get_running_loop()
                    def output(text):
                        loop.call_soon_threadsafe(self.client.show, text)
                    await self._run_mutation(attach_agent, agent, output=output)
                finally:
                    self.client.resume_output()
            elif action == 'spawn':
                agent = await self._run_mutation(self.api.action, ws_id, 'spawn', body={
                    'provider': payload['provider'], 'cwd': payload['cwd'],
                    'history_mode': payload['history_mode'], 'name': payload['name']})
                agent_id = agent['agent_id']
                if self._accept_snapshot(version):
                    self._store_agent(agent)
                if (payload['provider'] == 'claude'
                        and not (Path(payload['cwd']) / '.claude').exists()):
                    self._notice(f'Claude may be waiting at a trust prompt; use '
                                 f'/attach {shlex.quote(str(_agent_label(agent)))} to answer it.')
            elif action == 'rename_session':
                result = await self._run_mutation(self.api.rename, ws_id, payload['name'])
                if self._accept_snapshot(version):
                    self.workspace = result
                    self._state_revision += 1
                    self._notify_view('action')
                    self._notice(f'Session renamed: {self.workspace.get("name", ws_id)}')
                else:
                    self._notice('Session rename completed; refreshing session state.')
            elif action == 'unread':
                data = await asyncio.to_thread(self.api.unread, ws_id, agent_id)
                self._notice(format_workspace_result('unread', WorkspaceCommandResult(data, workspace)))
            else:
                kwargs = {}
                if action == 'resume':
                    kwargs['body'] = {'fresh': payload['fresh'], 'name': payload['name'], 'cwd': payload['cwd']}
                elif action == 'history':
                    kwargs['body'] = {'mode': payload['mode']}
                result = await self._run_mutation(self.api.action, ws_id, action, agent_id, **kwargs)
                if action == 'retry':
                    self._notice('Retry requested.')
                elif self._accept_snapshot(version):
                    self._store_agent(result)
            return ActionOutcome('completed', workspace_id=ws_id, agent_id=agent_id)
        except (CLIError, OSError, TimeoutError) as error:
            resume = ((_RESUME_COMMAND.get() or argparse.Namespace(agent=agent_id, fresh=payload['fresh'],
                      agent_name=payload['name'], cwd=payload['cwd'])) if action == 'resume' else None)
            return self._failed_action(error, ws_id, agent_id, resume=resume)

    async def dispatch_action(self, text):
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None
        command = parts[0]
        if command not in SESSION_COMMANDS or command in ('/sessions', '/archive'):
            return None
        if command == '/history' and (len(parts) == 1 or self.workspace is None):
            return None
        ws_id = self.workspace['id'] if self.workspace is not None else None
        generation = self._selection_version
        try:
            if self.plain_channel or self.workspace is None:
                raise CLIError('This command requires a selected session; start chat with --session or the session picker.')
            try:
                words = shlex.split(text)
            except ValueError as error:
                raise CLIError(str(error)) from None
            args = _parse_command(command, words[1:])
            mode = getattr(args, 'history_mode', None)
            if mode is not None:
                _validate_history(mode)
            if command in ('/spawn', '/resume', '/attach'):
                require_tmux_platform()
            action = command[1:]
            defaulted_spawn = command == '/spawn' and (args.cwd is None or mode is None)
            if command == '/spawn':
                if args.cwd is None:
                    default = next((a['cwd'] for a in reversed(self.workspace['agents']) if a.get('cwd')),
                                   None) or str(Path.cwd())
                    args.cwd = default if self.presentation is not None else (
                        (await self.prompt('Working directory:', default=default)).strip() or default)
                if mode is None:
                    mode = 'literal' if self.presentation is not None else (
                        (await self.prompt('History mode [none/literal]:', default='literal')).strip() or 'literal')
                    _validate_history(mode)
                payload = {'provider': args.provider, 'cwd': args.cwd, 'history_mode': mode, 'name': args.agent_name}
            elif command == '/rename':
                action, payload = 'rename_session', {'name': args.name}
            else:
                agent_id = resolve_agent(self.workspace, args.agent)['agent_id'] if args.agent else None
                payload = {'agent_id': agent_id}
                if command == '/resume':
                    payload.update(fresh=args.fresh, name=args.agent_name, cwd=args.cwd)
                elif command == '/history':
                    payload['mode'] = mode
            if generation != self._selection_version:
                return ActionOutcome('cancelled', workspace_id=ws_id)
            # Task-local diagnostics retain the user's selector without extending payloads.
            token = _RESUME_COMMAND.set(args if command == '/resume' else None)
            try:
                outcome = await self.execute_action(action, payload)
            finally:
                _RESUME_COMMAND.reset(token)
            if self.presentation is not None and defaulted_spawn and outcome.status == 'completed':
                message = _safe(f'Spawn defaults used: cwd {args.cwd}; history mode {mode}. '
                                'Change with --cwd/--history-mode or New agent.')
                self._notice(message)
                return ActionOutcome(outcome.status, message, outcome.workspace_id, outcome.agent_id)
            return outcome
        except (CLIError, OSError, TimeoutError) as error:
            return self._failed_action(error, ws_id)

    async def handle(self, text):
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None
        command = parts[0]
        if command in ('/quit', '/exit'):
            return 'quit'
        if command == '/history' and (len(parts) == 1 or self.workspace is None):
            return None
        if self.workspace is not None and command in ('/agents', '/help'):
            if command == '/agents':
                self._show_agents()
            else:
                self.client.show(SESSION_HELP)
            return 'continue'
        if command in ('/join', '/create') and self.workspace is not None:
            self.client.show('Use /sessions to switch sessions, or plain --channel mode to join/create channels.')
            return 'continue'
        if command not in SESSION_COMMANDS:
            return None
        switching = False
        try:
            if command not in ('/sessions', '/archive'):
                await self.dispatch_action(text)
                return 'continue'
            if self.plain_channel or self.workspace is None:
                raise CLIError('This command requires a selected session; start chat with --session or the session picker.')
            try:
                words = shlex.split(text)
            except ValueError as error:
                raise CLIError(str(error)) from None
            _parse_command(command, words[1:])
            generation = self._selection_version
            if command == '/sessions':
                await self.close()
                switching = True
                return await self._pick_again()
            answer = (await self.prompt('Archive session? [y/N]', default='n')).strip().lower()
            if answer not in ('y', 'yes') or generation != self._selection_version:
                return 'continue'
            outcome = await self.execute_action('archive_session', {'confirmed': True})
            if outcome.status != 'completed':
                return 'continue'
            switching = True
            return await self._pick_again()
        except CLIError as error:
            self.client.show(str(error))
            if switching and self.workspace is None:
                return 'quit'
        except EOFError:
            return 'quit'
        except KeyboardInterrupt:
            return 'continue'
        except (OSError, TimeoutError):
            self.client.show('Session request failed or timed out. Check the local server and retry.')
            if switching and self.workspace is None:
                return 'quit'
        return 'continue'
