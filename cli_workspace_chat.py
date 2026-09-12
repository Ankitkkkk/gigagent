"""Interactive server startup and terminal session selection."""

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
from cli_workspaces import WINDOWS_TMUX_ERROR, require_tmux_platform, resolve_session


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
    hint = (f'Start it manually: python run.py\nServer log: {_safe(log_path)}\n'
            f'Tmux session: {SERVER_SESSION}')

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
            raise CLIError(f'Tmux session {SERVER_SESSION} already exists but the server is unavailable.\n' + hint)
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
            raise CLIError(_safe(error) + '\n' + hint) from None
        if status is not None:
            output(f'Started server in tmux session {SERVER_SESSION}.')
            return ready(status)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.25, remaining))
    raise CLIError('The local server did not become ready within 15 seconds.\n' + hint)


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
    eligible = [(agent, cwd) for agent in agents
                if agent.get('last_state') == 'exited'
                and (cwd := _agent_cwd(agent)) is not None]
    if no_resume or not eligible:
        return workspace
    answer = (await prompt(f'Resume {len(eligible)} stopped agents? [Y/n]', default='y')).strip().lower()
    if answer not in ('', 'y', 'yes'):
        return workspace
    require_tmux_platform()
    resumed = False
    for agent, cwd in eligible:
        try:
            await asyncio.to_thread(api.action, workspace['id'], 'resume', agent['agent_id'],
                                    body={'cwd': str(cwd)})
            resumed = True
        except CLIError as error:
            output(_safe(error))
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


class WorkspaceChatController:
    def __init__(self, client, api, *, selector=None, no_resume=False, plain_channel=False):
        self.client = client
        self.api = api
        self.selector = selector
        self.no_resume = no_resume
        self.plain_channel = plain_channel
        self.workspace = None

    async def initialize(self, prompt):
        if self.plain_channel:
            return True
        self.workspace = await choose_workspace(
            self.api, prompt, self.client.show, selector=self.selector, no_resume=self.no_resume)
        if self.workspace is None:
            return False
        self.client.channel = self.workspace['channel']
        return True
