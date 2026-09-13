"""Interactive server startup and terminal session selection."""

import argparse
import asyncio
from collections import Counter
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
from cli_view_contracts import ViewEvent


ROOT = Path(__file__).resolve().parent
SERVER_SESSION = 'agentchattr-server'


def _safe(value):
    return ''.join(c for c in str(value) if c.isprintable() and c != '\x1b')


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


async def _offer_resume(api, workspace, prompt, output, no_resume):
    agents = workspace.get('agents', [])
    for agent in agents:
        output(_agent_line(agent))
    eligible = [agent for agent in agents
                if agent.get('last_state') == 'exited'
                and _agent_cwd(agent) is not None]
    if no_resume or not eligible:
        return workspace
    if sys.platform == 'win32':
        output(WINDOWS_TMUX_ERROR)
        return workspace
    answer = (await prompt(f'Resume {len(eligible)} stopped agents? [Y/n]', default='y')).strip().lower()
    if answer not in ('', 'y', 'yes'):
        return workspace
    require_tmux_platform()
    resumed = False
    for agent in eligible:
        try:
            await asyncio.to_thread(api.action, workspace['id'], 'resume', agent['agent_id'],
                                    body={})
            resumed = True
        except CLIError as error:
            output(_safe(error))
            if error.status == 409 and '--fresh' in str(error):
                output(_safe(f'/resume {shlex.quote(str(_agent_label(agent)))} --fresh'))
    if resumed:
        workspace = await asyncio.to_thread(api.get, workspace['id'])
    return workspace


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
                answer = (await prompt('Unarchive it? [y/N]', default='n')).strip().lower()
                if answer not in ('y', 'yes'):
                    if selector is not None:
                        raise CLIError('Archived session was not selected')
                    continue
                selected = await asyncio.to_thread(api.action, selected['id'], 'unarchive')
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

    def _select(self, workspace):
        self.workspace = workspace
        self._selection_version += 1
        self._closed = False
        self._failed_launches.clear()
        self._refresh_requested = workspace is not None
        self._poll_error = None
        self._agent_states = {a['agent_id']: self._agent_status(a)
                              for a in (workspace or {}).get('agents', [])}
        if workspace is not None:
            self.client.channel = workspace['channel']
            self.client.pending_channel = None
        self._notify_view('selection')

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
        self.client.show(shlex.join(words))

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
        args = None
        switching = False
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
            ws_id = self.workspace['id']
            if command == '/attach':
                agent = resolve_agent(self.workspace, args.agent)
                self.client.pause_output()
                try:
                    await asyncio.to_thread(attach_agent, agent, output=self.client.show)
                finally:
                    self.client.resume_output()
                return 'continue'
            if command == '/sessions':
                await self.close()
                switching = True
                return await self._pick_again()
            if command == '/archive':
                answer = (await self.prompt('Archive session? [y/N]', default='n')).strip().lower()
                if answer not in ('y', 'yes'):
                    return 'continue'
                await asyncio.to_thread(self.api.action, ws_id, 'archive')
                switching = True
                return await self._pick_again()
            if command == '/spawn':
                if args.cwd is None:
                    default = next((a['cwd'] for a in reversed(self.workspace['agents']) if a.get('cwd')),
                                   None) or str(Path.cwd())
                    args.cwd = (await self.prompt('Working directory:', default=default)).strip() or default
                if mode is None:
                    mode = (await self.prompt('History mode [none/literal]:', default='literal')).strip() or 'literal'
                    _validate_history(mode)
                version = self._snapshot_version()
                agent = await asyncio.to_thread(self.api.action, ws_id, 'spawn', body={
                    'provider': args.provider, 'cwd': args.cwd, 'history_mode': mode,
                    'name': args.agent_name})
                if self._accept_snapshot(version):
                    self._store_agent(agent)
                if args.provider == 'claude' and not (Path(args.cwd) / '.claude').exists():
                    self.client.show(f'Claude may be waiting at a trust prompt; use '
                                     f'/attach {shlex.quote(str(_agent_label(agent)))} to answer it.')
            elif command == '/rename':
                version = self._snapshot_version()
                result = await asyncio.to_thread(self.api.rename, ws_id, args.name)
                if self._accept_snapshot(version):
                    self.workspace = result
                    self._state_revision += 1
                    self.client.show(f'Session renamed: {self.workspace.get("name", ws_id)}')
                else:
                    self.client.show('Session rename completed; refreshing session state.')
            elif command == '/unread':
                agent_id = resolve_agent(self.workspace, args.agent)['agent_id'] if args.agent else None
                data = await asyncio.to_thread(self.api.unread, ws_id, agent_id)
                self.client.show(format_workspace_result('unread', WorkspaceCommandResult(data, self.workspace)))
            elif command in ('/resume', '/stop', '/history', '/retry'):
                agent_id = resolve_agent(self.workspace, args.agent)['agent_id']
                kwargs = {}
                if command == '/resume':
                    kwargs['body'] = {'fresh': args.fresh, 'name': args.agent_name, 'cwd': args.cwd}
                elif command == '/history':
                    kwargs['body'] = {'mode': mode}
                version = self._snapshot_version()
                result = await asyncio.to_thread(self.api.action, ws_id, command[1:], agent_id, **kwargs)
                if command == '/retry':
                    self.client.show('Retry requested.')
                elif self._accept_snapshot(version):
                    self._store_agent(result)
        except CLIError as error:
            self.client.show(str(error))
            if command == '/resume' and args is not None:
                self._resume_hint(args, error)
            if switching and self.workspace is None:
                return 'quit'
        except EOFError:
            return 'quit'
        except KeyboardInterrupt:
            return 'continue'
        except (OSError, TimeoutError):
            # Transport exception text can contain authenticated URLs.
            self.client.show('Session request failed or timed out. Check the local server and retry.')
            if switching and self.workspace is None:
                return 'quit'
        return 'continue'
