"""Interactive startup and session selection boundary regressions."""

import asyncio
import builtins
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs

import cli_api
import cli
from cli import ChatClient
from cli_api import CLIError
from cli_workspaces import WorkspaceAPI

try:
    import cli_workspace_chat as chat
except ImportError:
    chat = None


class Response(io.BytesIO):
    def __init__(self, url, data):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(chat, "startup module is not implemented")
        self.tmp = tempfile.TemporaryDirectory(prefix="cli startup ' ")
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.config = {'server': {'port': 18300, 'data_dir': str(self.data)},
                       'images': {'upload_dir': str(Path(self.tmp.name) / 'uploads')},
                       'mcp': {'http_port': 18200, 'sse_port': 18201}}
        self.url = 'http://127.0.0.1:18300'
        self.status = {'paused': False, 'data_dir': str(self.data), 'claude': {}}
        self.output = []
        self.requests = []
        self.responses = []
        self.now = 100.0
        self.opener = Mock()
        self.opener.open.side_effect = self.open
        self.addCleanup(patch.stopall)
        patch('cli_api._opener', return_value=self.opener).start()
        self.runner = patch('cli_workspace_chat.subprocess.run', side_effect=[
            subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0)]).start()
        patch('cli_workspace_chat.shutil.which', return_value='/usr/bin/tmux').start()
        patch('cli_workspace_chat.sys.platform', 'linux').start()
        patch('cli_workspace_chat.time.monotonic', side_effect=lambda: self.now).start()
        patch('cli_workspace_chat.time.sleep', side_effect=self.sleep).start()

    def sleep(self, seconds):
        self.assertLessEqual(seconds, .25)
        self.now += seconds

    def open(self, request, timeout):
        url = request if isinstance(request, str) else request.full_url
        self.requests.append((request, timeout))
        if self.responses:
            value = self.responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return Response(url, value)
        if url.endswith('/'):
            return Response(url, b'<script>window.__SESSION_TOKEN__="fresh-secret";</script>')
        return Response(url, json.dumps(self.status).encode())

    def ensure(self, explicit=False):
        return chat.ensure_server(self.url, explicit_url=explicit,
                                  config=self.config, output=self.output.append)

    def down(self):
        self.responses.append(URLError(ConnectionRefusedError()))

    def assert_manual(self, error):
        self.assertIn('Start it manually: python run.py', str(error.exception))
        self.assertIn(str(self.data / 'logs/server.log'), str(error.exception))

    def test_running_status_is_authenticated_without_launch(self):
        self.assertEqual(self.ensure(), self.status)
        self.runner.assert_not_called()
        self.assertEqual(self.requests[1][0].get_header('X-session-token'), 'fresh-secret')
        self.assertEqual(self.requests[1][0].full_url, self.url + '/api/status')

    def test_explicit_url_down_never_launches(self):
        self.down()
        with self.assertRaises(CLIError) as error:
            self.ensure(explicit=True)
        self.assert_manual(error)
        self.assertNotIn('agentchattr-server', str(error.exception))
        self.runner.assert_not_called()

    def test_windows_refuses_autostart(self):
        self.down()
        with patch('cli_workspace_chat.sys.platform', 'win32'):
            with self.assertRaisesRegex(CLIError, r'^Requires tmux \(Linux/macOS\)\. See wrapper_windows.py for manual launch\.$'):
                self.ensure()
        self.runner.assert_not_called()

    def test_missing_tmux_has_manual_log_hint(self):
        self.down()
        with patch('cli_workspace_chat.shutil.which', return_value=None):
            with self.assertRaises(CLIError) as error:
                self.ensure()
        self.assert_manual(error)
        self.assertNotIn('agentchattr-server', str(error.exception))
        self.runner.assert_not_called()

    def test_launch_quotes_all_resolved_config_flags(self):
        self.down()
        self.assertEqual(self.ensure(), self.status)
        calls = [call.args[0] for call in self.runner.call_args_list]
        self.assertEqual(calls[0], ['tmux', 'has-session', '-t', '=agentchattr-server'])
        self.assertEqual(calls[1][:7], ['tmux', 'new-session', '-d', '-s',
                         'agentchattr-server', '-c', str(Path(chat.__file__).resolve().parent)])
        args = shlex.split(calls[1][7])
        root = Path(chat.__file__).resolve().parent
        self.assertEqual(args[1], str(root / 'run.py'))
        for flag, value in [('--port', '18300'), ('--data-dir', str(self.data)),
                            ('--upload-dir', self.config['images']['upload_dir']),
                            ('--mcp-http-port', '18200'), ('--mcp-sse-port', '18201')]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(args[-3:], ['>>', str(self.data / 'logs/server.log'), '2>&1'])
        self.assertTrue((self.data / 'logs').is_dir())
        self.assertIn('Started server in tmux session agentchattr-server.', self.output)

    def test_relative_paths_resolve_against_module_root(self):
        self.down()
        self.config['server']['data_dir'] = 'relative-data'
        self.config['images']['upload_dir'] = 'relative-uploads'
        with patch.object(Path, 'mkdir'):
            self.ensure()
        args = shlex.split(self.runner.call_args.args[0][-1])
        root = Path(chat.__file__).resolve().parent
        self.assertEqual(args[args.index('--data-dir') + 1], str(root / 'relative-data'))
        self.assertEqual(args[args.index('--upload-dir') + 1], str(root / 'relative-uploads'))

    def test_existing_named_session_refuses_even_if_status_would_become_ready(self):
        self.down()
        self.runner.side_effect = [subprocess.CompletedProcess([], 0)]
        with self.assertRaises(CLIError) as error:
            self.ensure()
        self.assert_manual(error)
        self.assertIn('agentchattr-server', str(error.exception))
        self.assertEqual(len(self.runner.call_args_list), 1)
        self.assertEqual(len(self.requests), 1)

    def test_launch_failure_has_manual_log_hint_without_unconfirmed_tmux(self):
        self.down()
        self.runner.side_effect = [subprocess.CompletedProcess([], 1),
                                   subprocess.CompletedProcess([], 1)]
        with self.assertRaises(CLIError) as error:
            self.ensure()
        self.assert_manual(error)
        self.assertNotIn('agentchattr-server', str(error.exception))
        self.assertNotIn('Started server in tmux session agentchattr-server.', self.output)

    def test_readiness_timeout_names_only_confirmed_remaining_tmux_session(self):
        self.opener.open.side_effect = URLError(ConnectionRefusedError())
        for returncode in (0, 1):
            with self.subTest(session_exists=returncode == 0):
                self.runner.reset_mock(side_effect=True)
                self.runner.side_effect = [subprocess.CompletedProcess([], 1),
                    subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], returncode)]
                with self.assertRaises(CLIError) as error:
                    self.ensure()
                self.assert_manual(error)
                self.assertEqual('Tmux session: agentchattr-server' in str(error.exception), returncode == 0)
                self.assertEqual(self.runner.call_args.args[0],
                                 ['tmux', 'has-session', '-t', '=agentchattr-server'])
                self.assertLessEqual(self.runner.call_args.kwargs['timeout'], 5)

    def test_fifteen_second_deadline_bounds_every_probe(self):
        def unreachable(request, timeout):
            self.requests.append((request, timeout))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2)
            if len(self.requests) > 1:
                self.assertLessEqual(timeout, 117 - self.now)
            self.now += timeout
            raise URLError(TimeoutError())
        self.opener.open.side_effect = unreachable
        with self.assertRaises(CLIError) as error:
            self.ensure()
        self.assert_manual(error)
        self.assertEqual(self.now, 117)

    def test_foreign_data_directory_warns_and_continues(self):
        self.status['data_dir'] = '/other/server/data'
        self.assertEqual(self.ensure(), self.status)
        rendered = '\n'.join(self.output)
        self.assertIn('warning', rendered.lower())
        self.assertIn('/other/server/data', rendered)
        self.assertIn(str(self.data), rendered)

    def test_responding_foreign_endpoints_never_launch(self):
        for body in [b'<html>Other server</html>', b'bad\xff']:
            with self.subTest(body=body):
                self.responses = [body]
                with self.assertRaises(CLIError):
                    self.ensure()
                self.runner.assert_not_called()

    def test_invalid_status_shape_never_launches(self):
        for status in [{}, {'data_dir': '/tmp'}, [], {'paused': False, 'data_dir': 3}]:
            with self.subTest(status=status):
                self.status = status
                with self.assertRaisesRegex(CLIError, 'agentchattr'):
                    self.ensure()
                self.runner.assert_not_called()

    def test_http_auth_failure_never_launches(self):
        self.responses = [b'<script>window.__SESSION_TOKEN__="token";</script>',
                          HTTPError(self.url + '/api/status', 403, 'Forbidden', {}, io.BytesIO(b'{}'))]
        with self.assertRaises(CLIError):
            self.ensure()
        self.runner.assert_not_called()


class PickerFixture:
    def setUp(self):
        self.assertTrue(hasattr(chat, 'choose_workspace'), 'picker is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.workspaces = []
        self.output = []
        self.prompts = []
        self.calls = []
        self.warning = None
        self.main_thread = threading.get_ident()
        self.refuse = set()
        self.refusal = CLIError('native session id unknown; use --fresh', 409)
        self.api = WorkspaceAPI('http://127.0.0.1:18300')
        self.addCleanup(patch.stopall)
        patch('cli_api.fetch_session_token', return_value='token').start()
        patch('cli_api.request_json', side_effect=self.transport).start()
        patch('cli_workspace_chat.time.time', return_value=1760000000).start()

    def workspace(self, **changes):
        ws = {'id': 'ws_abcd00', 'name': 'billing', 'channel': 'ws-billing',
              'archived': False, 'agents': [], 'updated_at': '2025-10-09T06:53:20Z'}
        ws.update(changes)
        return ws

    def agent(self, **changes):
        agent = {'agent_id': 'ag_a', 'registry_name': 'claude-1', 'provider': 'claude',
                 'cwd': str(self.tmp_path), 'last_state': 'exited',
                 'native_session_id': None, 'unread_count': 0,
                 'last_launch': None, 'history_mode': 'literal', 'history_state': 'done'}
        agent.update(changes)
        return agent

    def transport(self, url, token, method, path, body=None, timeout=5):
        self.assertNotEqual(threading.get_ident(), self.main_thread,
                            'synchronous API request blocked the event loop')
        self.assertEqual(token, 'token')
        self.calls.append((method, path, body))
        parsed = urlsplit(path)
        if parsed.path == '/api/status':
            return {'paused': False, 'data_dir': str(self.tmp_path)}
        if parsed.path == '/api/workspaces':
            if method == 'GET':
                archived = parse_qs(parsed.query)['include_archived'] == ['1']
                return {'workspaces': [dict(ws) for ws in self.workspaces if archived or not ws['archived']],
                        'warning': self.warning}
            if method == 'POST':
                ws = self.workspace(id='ws_new', name=body['name'] or 'ws_new', channel='ws-new')
                self.workspaces.append(ws)
                return dict(ws)
        parts = parsed.path.strip('/').split('/')
        ws = next(ws for ws in self.workspaces if ws['id'] == parts[2])
        if method == 'GET':
            return dict(ws)
        if parts[-1] == 'unarchive':
            ws['archived'] = False
            return dict(ws)
        if parts[-1] == 'resume':
            agent = next(a for a in ws['agents'] if a['agent_id'] == parts[-2])
            if agent['agent_id'] in self.refuse:
                raise self.refusal
            agent['last_state'] = 'starting'
            return dict(agent)
        if parts[-1] == 'checkpoint':
            return {'checked': 0}
        raise AssertionError(f'Unexpected request: {self.calls[-1]}')

    def answers(self, *answers):
        values = iter(answers)
        async def prompt(text, default=''):
            self.prompts.append((text, default))
            value = next(values)
            if isinstance(value, BaseException):
                raise value
            return value
        return prompt

    async def choose(self, *answers, **kwargs):
        return await chat.choose_workspace(self.api, self.answers(*answers),
                                           self.output.append, **kwargs)


class PickerTests(PickerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_empty_list_immediately_asks_name_and_blank_uses_server_id(self):
        selected = await self.choose('', no_resume=True)
        self.assertEqual(selected['name'], 'ws_new')
        self.assertEqual(self.prompts, [('Session name:', '')])
        self.assertEqual(self.calls[-1], ('POST', '/api/workspaces', {'name': ''}))
        self.assertNotIn('Choose:', [p[0] for p in self.prompts])

    async def test_new_session_menu_creates_named_session(self):
        self.workspaces = [self.workspace()]
        selected = await self.choose('n', 'billing', no_resume=True)
        self.assertEqual(selected['id'], 'ws_new')
        self.assertEqual(self.calls[-1], ('POST', '/api/workspaces', {'name': 'billing'}))
        rendered = '\n'.join(self.output)
        for text in ['Sessions', 'n. New session', 'a. Show archived']:
            self.assertIn(text, rendered)

    async def test_numeric_selection_sorts_newest_and_stably_breaks_ties(self):
        self.workspaces = [self.workspace(id='ws_000011', name='same', updated_at='2025-10-07T08:53:20Z'),
                           self.workspace(id='ws_abcd22', name='same'),
                           self.workspace(id='ws_cdef33', name='last')]
        selected = await self.choose('1', no_resume=True)
        self.assertEqual(selected['id'], 'ws_abcd22')
        rendered = '\n'.join(self.output)
        self.assertIn('1. same (abcd)', rendered)
        self.assertIn('2. last', rendered)
        self.assertIn('3. same (0000)', rendered)
        self.assertIn('2h ago', rendered)
        self.assertIn('2d ago', rendered)

    async def test_all_agents_states_unread_unknown_id_fresh_and_warning_are_safe(self):
        self.workspaces = [self.workspace(name='billing\x1b[2J', agents=[
            self.agent(last_state='running', native_session_id='known'),
            self.agent(agent_id='ag_b', registry_name='codex-1', unread_count=3),
            self.agent(agent_id='ag_c', registry_name='gemini-1', last_state='starting',
                       last_launch={'kind': 'fresh'}, history_state='pending')])]
        self.warning = 'recovered warning\x1b[2J'
        await self.choose('1', no_resume=True)
        rendered = '\n'.join(self.output)
        for text in ['claude-1 running', 'codex-1 exited (unread 3)', 'id unknown',
                     'gemini-1 fresh', 'catching up…', 'recovered warning']:
            self.assertIn(text, rendered)
        self.assertNotIn('\x1b', rendered)

    async def test_show_archived_decline_returns_picker_without_mutation(self):
        self.workspaces = [self.workspace(), self.workspace(id='ws_old', name='old', archived=True)]
        selected = await self.choose('a', '2', 'n', '1', no_resume=True)
        self.assertEqual(selected['id'], 'ws_abcd00')
        self.assertIn(('Unarchive it? [y/N]', 'n'), self.prompts)
        self.assertIn('archived', self.output)
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))
        self.assertIn(('GET', '/api/workspaces?include_archived=1', None), self.calls)

    async def test_archived_yes_unarchives_then_selects(self):
        self.workspaces = [self.workspace(), self.workspace(id='ws_old', archived=True)]
        selected = await self.choose('a', '2', 'y', no_resume=True)
        self.assertEqual(selected['id'], 'ws_old')
        self.assertFalse(selected['archived'])
        self.assertIn(('POST', '/api/workspaces/ws_old/unarchive', None), self.calls)

    async def test_explicit_archived_decline_fails(self):
        self.workspaces = [self.workspace(archived=True)]
        with self.assertRaises(CLIError):
            await self.choose('n', selector='billing')
        self.assertEqual(self.prompts, [('Unarchive it? [y/N]', 'n')])
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))

    async def test_toggle_archived_back_hides_archived(self):
        self.workspaces = [self.workspace()]
        await self.choose('a', 'a', '1', no_resume=True)
        lists = [c[1] for c in self.calls if '?' in c[1]]
        self.assertEqual(lists, ['/api/workspaces?include_archived=0',
                                '/api/workspaces?include_archived=1',
                                '/api/workspaces?include_archived=0'])

    async def test_invalid_selection_reprompts(self):
        self.workspaces = [self.workspace()]
        selected = await self.choose('0', '300', 'bad', '1', no_resume=True)
        self.assertEqual(selected['id'], 'ws_abcd00')
        self.assertEqual(len(self.prompts), 4)

    async def test_eof_or_cancel_does_not_create(self):
        for cancellation in [EOFError(), KeyboardInterrupt()]:
            with self.subTest(cancellation=type(cancellation).__name__):
                self.assertIsNone(await self.choose(cancellation))
                self.assertFalse(any(c[0] == 'POST' for c in self.calls))


class ResumeTests(PickerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_picker_exact_menu_and_resume_prompt(self):
        self.workspaces = [self.workspace(agents=[self.agent()])]
        selected = await self.choose('1', 'n')
        self.assertEqual(selected['id'], 'ws_abcd00')
        self.assertEqual(self.prompts, [('Choose:', ''), ('Resume 1 stopped agents? [Y/n]', 'y')])
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))

    async def test_missing_cwd_warning_skips_resume(self):
        file_path = self.tmp_path / 'file'
        file_path.write_text('not a directory')
        for cwd in ['/definitely/missing/cli-cwd', str(file_path), None, '']:
            with self.subTest(cwd=cwd):
                self.workspaces = [self.workspace(agents=[self.agent(cwd=cwd)])]
                await self.choose('1')
                self.assertIn('⚠ cwd missing — /resume claude-1 --cwd PATH', '\n'.join(self.output))
                self.assertFalse(any(c[0] == 'POST' for c in self.calls))
                self.assertFalse(any(p[0].startswith('Resume ') for p in self.prompts))

    async def test_batch_default_yes_only_resumes_eligible_exited_and_refreshes(self):
        self.workspaces = [self.workspace(agents=[self.agent(),
            self.agent(agent_id='ag_b', registry_name='codex-1', native_session_id='known'),
            self.agent(agent_id='ag_c', last_state='running'),
            self.agent(agent_id='ag_d', last_state='starting'),
            self.agent(agent_id='ag_e', cwd='/definitely/missing/cli-cwd')])]
        selected = await self.choose('1', '')
        self.assertEqual(self.prompts[-1], ('Resume 2 stopped agents? [Y/n]', 'y'))
        posts = [c for c in self.calls if c[0] == 'POST']
        self.assertEqual(posts, [
            ('POST', '/api/workspaces/ws_abcd00/agents/ag_a/resume', {}),
            ('POST', '/api/workspaces/ws_abcd00/agents/ag_b/resume', {})])
        self.assertEqual(self.calls[-1], ('GET', '/api/workspaces/ws_abcd00', None))
        self.assertEqual(selected['agents'][0]['last_state'], 'starting')

    async def test_batch_resume_does_not_override_stored_symlink_cwd(self):
        project = self.tmp_path / 'project'
        project.mkdir()
        link = self.tmp_path / 'project-link'
        link.symlink_to(project, target_is_directory=True)
        self.workspaces = [self.workspace(agents=[self.agent(cwd=str(link))])]
        await self.choose('1', 'yes')
        self.assertIn(('POST', '/api/workspaces/ws_abcd00/agents/ag_a/resume', {}), self.calls)

    async def test_resume_refusal_prints_exact_fresh_hint_and_continues(self):
        self.workspaces = [self.workspace(agents=[self.agent(),
            self.agent(agent_id='ag_b', registry_name='codex-1')])]
        self.refuse.add('ag_a')
        await self.choose('1', 'yes')
        self.assertIn('native session id unknown; use --fresh', self.output)
        self.assertIn('/resume claude-1 --fresh', self.output)
        self.assertEqual([c[1] for c in self.calls if c[0] == 'POST'], [
            '/api/workspaces/ws_abcd00/agents/ag_a/resume', '/api/workspaces/ws_abcd00/agents/ag_b/resume'])

    async def test_other_resume_refusals_do_not_recommend_fresh(self):
        for status, message in [(409, 'name claude-1 in use'), (409, 'agent is already running'),
                                (403, 'authentication required'), (500, 'failed despite --fresh')]:
            with self.subTest(status=status, message=message):
                self.workspaces = [self.workspace(agents=[self.agent()])]
                self.refuse = {'ag_a'}
                self.refusal = CLIError(message, status)
                self.output.clear()
                await self.choose('1', 'yes')
                self.assertIn(message, self.output)
                self.assertNotIn('/resume claude-1 --fresh', self.output)

    async def test_session_selector_skips_picker_retains_resume_prompt(self):
        self.workspaces = [self.workspace(agents=[self.agent()])]
        await self.choose('n', selector='bill')
        self.assertEqual(self.prompts, [('Resume 1 stopped agents? [Y/n]', 'y')])
        self.assertNotIn('Sessions', self.output)

    async def test_no_resume_suppresses_batch_question(self):
        self.workspaces = [self.workspace(agents=[self.agent()])]
        await self.choose(selector='billing', no_resume=True)
        self.assertEqual(self.prompts, [])
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))

    async def test_eof_at_resume_prompt_returns_none_without_resume(self):
        self.workspaces = [self.workspace(agents=[self.agent()])]
        self.assertIsNone(await self.choose('1', EOFError()))
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))


class ControllerTests(PickerFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(hasattr(chat, 'WorkspaceChatController'), 'controller is not implemented')
        self.client = ChatClient('http://127.0.0.1:18300', output=self.output.append)

    async def test_initialize_selects_channel_before_any_receiver(self):
        self.workspaces = [self.workspace(agents=[self.agent()])]
        controller = chat.WorkspaceChatController(self.client, self.api, selector='bill', no_resume=True)
        self.assertTrue(await controller.initialize(self.answers()))
        self.assertEqual(self.client.channel, 'ws-billing')
        self.assertEqual(controller.workspace['id'], 'ws_abcd00')
        self.assertEqual(self.prompts, [])

    async def test_windows_exited_agent_skips_resume_and_enters_chat(self):
        self.workspaces = [self.workspace(agents=[self.agent()])]
        controller = chat.WorkspaceChatController(self.client, self.api, selector='billing')
        with patch('cli_workspace_chat.sys.platform', 'win32'):
            initialized = await controller.initialize(self.answers(''))
        self.assertTrue(initialized)
        self.assertEqual(self.client.channel, 'ws-billing')
        self.assertIn('Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch.', self.output)
        self.assertEqual(self.prompts, [])
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))

    async def test_plain_channel_skips_picker_and_keeps_channel(self):
        self.client.channel = 'support'
        controller = chat.WorkspaceChatController(self.client, self.api, plain_channel=True)
        self.assertTrue(await controller.initialize(self.answers()))
        self.assertEqual(self.client.channel, 'support')
        self.assertIsNone(controller.workspace)
        self.assertEqual(self.calls, [])

    async def test_cancelled_initialization_does_not_enter_chat(self):
        controller = chat.WorkspaceChatController(self.client, self.api)
        self.assertFalse(await controller.initialize(self.answers(EOFError())))
        self.assertIsNone(controller.workspace)
        self.assertEqual(self.client.channel, 'general')


class InteractiveIntegrationTests(PickerFixture, unittest.IsolatedAsyncioTestCase):
    async def run_prompt(self, answers, *, controller=True):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        client = ChatClient('http://127.0.0.1:18300', output=self.output.append)
        receiver_channels = []
        async def receive():
            receiver_channels.append(client.channel)
            await asyncio.Future()
        client.receive_forever = receive
        selected = chat.WorkspaceChatController(client, self.api, no_resume=True)
        with create_pipe_input() as pipe:
            def session(**kwargs):
                return PromptSession(input=pipe, output=DummyOutput(), **kwargs)
            with patch('prompt_toolkit.PromptSession', side_effect=session) as factory:
                pipe.send_text(answers)
                async with asyncio.timeout(5):
                    if controller:
                        await cli.interactive(client, selected)
                    else:
                        await cli.interactive(client)
                self.assertEqual(factory.call_count, 1)
        return client, selected, receiver_channels

    async def test_picker_and_chat_share_prompt_and_receiver_starts_in_session(self):
        self.workspaces = [self.workspace()]
        client, controller, channels = await self.run_prompt('1\n/quit\n')
        self.assertEqual(client.channel, 'ws-billing')
        self.assertEqual(controller.workspace['id'], 'ws_abcd00')
        self.assertEqual(channels, ['ws-billing'])

    async def test_cancelled_picker_never_starts_receiver(self):
        _, controller, channels = await self.run_prompt('\x04')
        self.assertIsNone(controller.workspace)
        self.assertEqual(channels, [])
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))

    async def test_archived_confirmation_accepts_typed_yes_without_editing_default(self):
        self.workspaces = [self.workspace(), self.workspace(id='ws_old', archived=True)]
        _, controller, _ = await self.run_prompt('a\n2\ny\n\x04')
        self.assertIsNotNone(controller.workspace)
        self.assertEqual(controller.workspace['id'], 'ws_old')
        self.assertFalse(controller.workspace['archived'])

    async def test_direct_interactive_call_stays_plain(self):
        client, _, channels = await self.run_prompt('/quit\n', controller=False)
        self.assertEqual(client.channel, 'general')
        self.assertEqual(channels, ['general'])
        self.assertEqual(self.calls, [])


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.err = io.StringIO()
        self.out = io.StringIO()
        patch('cli.sys.stdin.isatty', return_value=True).start()
        self.interactive = patch('cli.interactive', new_callable=AsyncMock).start()
        self.config = {'server': {'port': 18300, 'data_dir': '/tmp/cli-main-data'},
                       'images': {'upload_dir': '/tmp/cli-main-uploads'},
                       'mcp': {'http_port': 18200, 'sse_port': 18201}}
        self.config_load = patch('cli.load_config', return_value=self.config).start()
        self.opener = Mock()
        self.opener.open.side_effect = self.open
        patch('cli_api._opener', return_value=self.opener).start()
        self.runner = patch('cli_workspace_chat.subprocess.run').start()

    def open(self, request, timeout):
        url = request if isinstance(request, str) else request.full_url
        if url.endswith('/'):
            return Response(url, b'<script>window.__SESSION_TOKEN__="token";</script>')
        return Response(url, json.dumps({'paused': False, 'data_dir': '/tmp/cli-main-data'}).encode())

    def main(self, *args):
        with redirect_stdout(self.out), redirect_stderr(self.err):
            cli.main(list(args))

    def test_default_chat_probes_status_and_passes_picker_controller(self):
        self.main()
        self.assertEqual(self.config_load.call_count, 1)
        self.assertEqual(self.opener.open.call_count, 2)
        self.runner.assert_not_called()
        args = self.interactive.await_args.args
        self.assertEqual(len(args), 2)
        self.assertIsInstance(args[1], chat.WorkspaceChatController)
        self.assertEqual(args[1].api.url, 'http://127.0.0.1:18300')
        self.assertFalse(args[1].plain_channel)

    def test_explicit_session_and_no_resume_are_carried_to_controller(self):
        self.main('chat', '--session', 'billing', '--no-resume')
        args = self.interactive.await_args.args
        self.assertEqual(len(args), 2)
        self.assertEqual(args[1].selector, 'billing')
        self.assertTrue(args[1].no_resume)

    def test_explicit_channel_is_plain_but_still_checks_server(self):
        self.main('chat', '--channel', 'support')
        self.assertEqual(self.opener.open.call_count, 2)
        args = self.interactive.await_args.args
        self.assertEqual(len(args), 2)
        self.assertTrue(args[1].plain_channel)
        self.assertEqual(args[0].channel, 'support')

    def test_explicit_url_down_never_launches_and_reports_log(self):
        self.opener.open.side_effect = URLError(ConnectionRefusedError())
        with self.assertRaises(SystemExit) as error:
            self.main('chat', '--url', 'http://127.0.0.1:18300')
        self.assertEqual(error.exception.code, 1)
        self.assertIn('Start it manually: python run.py', self.err.getvalue())
        self.assertIn('/tmp/cli-main-data/logs/server.log', self.err.getvalue())
        self.runner.assert_not_called()
        self.interactive.assert_not_awaited()

    def test_shell_command_never_calls_ensure_server(self):
        with patch.object(cli, 'ensure_server', side_effect=AssertionError('shell auto-start'), create=True) as ensure:
            self.main('status', '--json')
        ensure.assert_not_called()
        self.assertEqual(json.loads(self.out.getvalue())['paused'], False)
        self.runner.assert_not_called()

    def test_non_tty_rejects_before_config_http_or_process(self):
        with patch('cli.sys.stdin.isatty', return_value=False):
            with self.assertRaises(SystemExit) as error:
                self.main('chat')
        self.assertEqual(error.exception.code, 1)
        self.config_load.assert_not_called()
        self.opener.open.assert_not_called()
        self.runner.assert_not_called()

    def test_missing_interactive_dependency_rejects_before_side_effects(self):
        original_import = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == 'prompt_toolkit':
                raise ImportError('missing prompt dependency')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=missing):
            with self.assertRaises(SystemExit) as error:
                self.main('chat')
        self.assertEqual(error.exception.code, 1)
        self.assertIn('Install terminal dependencies', self.err.getvalue())
        self.config_load.assert_not_called()
        self.opener.open.assert_not_called()


class ControllerFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = self.tmp.name
        self.output = []
        self.client = ChatClient('http://127.0.0.1:18300', output=self.output.append)
        self.api = Mock(spec=WorkspaceAPI)
        self.agent = {'agent_id': 'ag_a', 'registry_name': 'claude-1',
                      'provider': 'claude', 'cwd': self.cwd, 'last_state': 'exited',
                      'native_session_id': 'native-a', 'unread_count': 0,
                      'history_mode': 'literal', 'history_state': 'done'}
        self.ws = {'id': 'ws_a', 'channel': 'ws-a', 'name': 'billing',
                   'archived': False, 'agents': [copy.deepcopy(self.agent)]}
        self.api.get.side_effect = lambda _: copy.deepcopy(self.ws)
        self.api.list.return_value = {'workspaces': [self.ws]}
        self.api.status.return_value = {'paused': False, 'data_dir': self.cwd}
        self.api.action.return_value = dict(self.agent, last_state='starting')
        self.controller = chat.WorkspaceChatController(self.client, self.api, no_resume=True)
        self.controller.workspace = copy.deepcopy(self.ws)
        self.controller.prompt = AsyncMock()

    async def handle(self, text):
        self.assertTrue(callable(getattr(self.controller, 'handle', None)),
                        'session command controller is not implemented')
        return await self.controller.handle(text)


class ControllerCommandTests(ControllerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_spawn_parses_quoted_values_and_stores_real_record(self):
        path = str(Path(self.cwd) / 'project space')
        self.api.action.return_value = dict(self.agent, agent_id='ag_new',
                                          registry_name='review person', last_state='starting')
        self.assertEqual(await self.handle(
            f'/spawn claude --agent-name "review person" --cwd {shlex.quote(path)} '
            '--history-mode none'), 'continue')
        self.api.action.assert_called_once_with('ws_a', 'spawn', body={
            'provider': 'claude', 'cwd': path, 'name': 'review person', 'history_mode': 'none'})
        self.assertEqual(self.controller.workspace['agents'][-1], self.api.action.return_value)
        self.assertIn('review person starting', '\n'.join(self.output))

    async def test_spawn_prompts_cwd_then_history_and_defaults(self):
        self.controller.prompt = AsyncMock(side_effect=['', ''])
        await self.handle('/spawn claude --agent-name reviewer')
        self.assertEqual(self.api.action.call_args.kwargs['body'], {
            'provider': 'claude', 'cwd': self.cwd, 'name': 'reviewer', 'history_mode': 'literal'})
        self.assertEqual([c.kwargs['default'] for c in self.controller.prompt.await_args_list],
                         [self.cwd, 'literal'])
        self.assertIn('trust', '\n'.join(self.output).lower())
        self.assertIn('/attach claude-1', '\n'.join(self.output))
        self.controller.workspace['agents'] = []
        self.controller.prompt = AsyncMock(side_effect=['', 'none'])
        with patch.object(chat.Path, 'cwd', return_value=Path(self.cwd) / 'fallback'):
            await self.handle('/spawn codex')
        self.assertEqual(self.api.action.call_args.kwargs['body']['cwd'],
                         str(Path(self.cwd) / 'fallback'))
        self.assertEqual(self.api.action.call_args.kwargs['body']['history_mode'], 'none')

    async def test_recorded_cwd_default_does_not_require_current_directory(self):
        self.controller.prompt = AsyncMock(side_effect=['', 'literal'])
        with patch.object(chat.Path, 'cwd', side_effect=FileNotFoundError):
            await self.handle('/spawn codex')
        self.api.action.assert_called_once_with('ws_a', 'spawn', body={
            'provider': 'codex', 'cwd': self.cwd, 'history_mode': 'literal', 'name': None})

    async def test_claude_existing_state_omits_trust_caveat(self):
        (Path(self.cwd) / '.claude').mkdir()
        await self.handle(f'/spawn claude --cwd {shlex.quote(self.cwd)} --history-mode literal')
        self.assertNotIn('trust', '\n'.join(self.output).lower())

    async def test_resume_flags_and_real_record_replace_selected_agent(self):
        await self.handle('/resume ag_a --agent-name "replacement agent" --fresh --cwd "/moved project"')
        self.api.action.assert_called_once_with('ws_a', 'resume', 'ag_a', body={
            'fresh': True, 'name': 'replacement agent', 'cwd': '/moved project'})
        self.assertEqual(self.controller.workspace['agents'], [self.api.action.return_value])

    async def test_stop_history_retry_unread_rename_contracts(self):
        await self.handle('/stop claude')
        self.api.action.assert_called_with('ws_a', 'stop', 'ag_a')
        await self.handle('/history claude-1 none')
        self.api.action.assert_called_with('ws_a', 'history', 'ag_a', body={'mode': 'none'})
        self.api.action.return_value = {'ok': True}
        await self.handle('/retry ag_a')
        self.api.action.assert_called_with('ws_a', 'retry', 'ag_a')
        self.api.unread.return_value = {'agents': [{'agent_id': 'ag_a', 'registry_name': 'claude-1',
            'count': 1, 'messages': [{'id': 7, 'sender': 'user', 'text': 'hello\x1b[2J',
                                     'routed_to': ['ag_a']}]}]}
        await self.handle('/unread claude')
        self.api.unread.assert_called_with('ws_a', 'ag_a')
        self.assertIn('unread 1', '\n'.join(self.output))
        self.assertIn('#7 user hello', '\n'.join(self.output))
        await self.handle('/unread')
        self.api.unread.assert_called_with('ws_a', None)
        renamed = dict(self.ws, name='new name')
        self.api.rename.return_value = renamed
        await self.handle('/rename "new name"')
        self.api.rename.assert_called_once_with('ws_a', 'new name')
        self.assertEqual(self.controller.workspace, renamed)
        self.assertNotIn('\x1b', '\n'.join(self.output))

    async def test_summary_refusal_has_exact_text(self):
        for command in ['/history claude-1 summary', '/spawn claude --history-mode summary']:
            await self.handle(command)
        self.assertEqual(self.output, [
            'summary history mode is not available in this version; use literal or none'] * 2)
        self.api.action.assert_not_called()
        self.controller.prompt.assert_not_awaited()

    async def test_invalid_syntax_has_no_api_calls_or_prompt(self):
        cases = [('/spawn', 'the following arguments are required: provider'),
                 ('/spawn "unterminated', 'No closing quotation'),
                 ('/spawn claude --bad', 'unrecognized arguments: --bad'),
                 ('/resume', 'the following arguments are required: agent'),
                 ('/resume ag_a extra', 'unrecognized arguments: extra'),
                 ('/stop ag_a extra', 'unrecognized arguments: extra'),
                 ('/retry', 'the following arguments are required: agent'),
                 ('/history claude literal extra', 'unrecognized arguments: extra'),
                 ('/history claude invalid', 'history mode must be literal or none'),
                 ('/history claude --mode none', 'unrecognized arguments: --mode'),
                 ('/unread --agent ag_a', 'unrecognized arguments: --agent'),
                 ('/rename', 'the following arguments are required: name'),
                 ('/archive extra', 'unrecognized arguments: extra'),
                 ('/sessions extra', 'unrecognized arguments: extra'),
                 ('/spawn claude --help', 'unrecognized arguments: --help')]
        for command, error in cases:
            with self.subTest(command=command):
                self.assertEqual(await self.handle(command), 'continue')
                self.assertEqual(self.output[-1], error)
        self.assertEqual(self.api.mock_calls, [])
        self.controller.prompt.assert_not_awaited()
        self.assertEqual(len(self.output), len(cases))

    async def test_windows_spawn_resume_refuse_before_mutation_or_prompt(self):
        with patch('cli_workspaces.os.name', 'nt'):
            await self.handle('/spawn claude')
            await self.handle('/resume ag_a')
        self.assertEqual(self.output, [chat.WINDOWS_TMUX_ERROR] * 2)
        self.api.action.assert_not_called()
        self.controller.prompt.assert_not_awaited()

    async def test_agent_resolution_is_workspace_scoped_and_ambiguous(self):
        self.controller.workspace['agents'].append(dict(self.agent, agent_id='ag_b', registry_name='claude-2'))
        await self.handle('/stop claude')
        self.assertIn('ambiguous agent claude', self.output[-1])
        self.assertIn('claude-1 (ag_a)', self.output[-1])
        self.assertIn('claude-2 (ag_b)', self.output[-1])
        await self.handle('/stop codex')
        self.assertIn('agent not found: codex', self.output[-1])
        self.api.action.assert_not_called()

    async def test_legacy_history_and_messages_delegate_join_create_blocked(self):
        for text in ['/history', 'hello "unterminated', '/continue', '/summary @claude']:
            self.assertIsNone(await self.handle(text))
        for text in ['/join general', '/create another']:
            self.assertEqual(await self.handle(text), 'continue')
            self.assertIn('/sessions', self.output[-1])
            self.assertIn('--channel', self.output[-1])
        self.assertEqual(self.api.mock_calls, [])

    async def test_plain_mode_rejects_session_commands(self):
        self.controller.plain_channel = True
        self.controller.workspace = None
        for text in ['/spawn claude', '/resume ag_a', '/stop ag_a', '/retry ag_a',
                     '/unread', '/rename name', '/archive', '/sessions']:
            self.assertEqual(await self.handle(text), 'continue')
            self.assertIn('session', self.output[-1].lower())
        self.assertIsNone(await self.handle('/history'))
        self.assertEqual(self.api.mock_calls, [])

    async def test_plain_mode_history_arguments_delegate_without_session_error_or_api(self):
        self.controller.plain_channel = True
        self.controller.workspace = None
        for text in ['/history 50', '/history ag_a none', '/history "unterminated']:
            self.assertIsNone(await self.handle(text))
        self.assertEqual(self.output, [])
        self.assertEqual(self.api.mock_calls, [])

    async def test_plain_mode_explanation_precedes_session_argument_parsing(self):
        self.controller.plain_channel = True
        self.controller.workspace = None
        for text in ['/spawn', '/spawn "unterminated']:
            self.assertEqual(await self.handle(text), 'continue')
            self.assertEqual(self.output[-1], 'This command requires a selected session; '
                             'start chat with --session or the session picker.')
        self.assertEqual(self.api.mock_calls, [])
        self.controller.prompt.assert_not_awaited()

    async def test_local_record_bug_is_not_disguised_as_transport_failure(self):
        self.api.action.return_value = {'last_state': 'running'}
        with self.assertRaises(KeyError):
            await self.handle('/stop claude')
        self.assertEqual(self.output, [])

    async def test_expected_failure_verbatim_and_transport_failure_secret_safe(self):
        self.api.action.side_effect = CLIError('specific refusal\x1b[2J', 400)
        await self.handle('/stop claude')
        self.assertEqual(self.output, ['specific refusal[2J'])
        self.api.action.side_effect = OSError('http://local?token=SECRET')
        await self.handle('/stop claude')
        self.assertNotIn('SECRET', '\n'.join(self.output))
        self.assertIn('Session request failed', self.output[-1])

    async def test_resume_recovery_hints_preserve_flags_and_match_refusal(self):
        cases = [('native session id unknown; use --fresh', 409, '--fresh'),
                 ('cwd missing; use --cwd PATH', 400, '--cwd PATH'),
                 ('name in use: held', 400, '--agent-name NAME')]
        for error, status, hint in cases:
            with self.subTest(error=error):
                self.api.action.side_effect = CLIError(error, status)
                await self.handle('/resume claude-1 --agent-name "new name" --cwd "/new cwd"')
                self.assertIn('/resume claude-1', self.output[-1])
                self.assertIn(hint, self.output[-1])
                if hint == '--fresh':
                    self.assertEqual(shlex.split(self.output[-1]), ['/resume', 'claude-1', '--fresh',
                        '--agent-name', 'new name', '--cwd', '/new cwd'])
        self.output.clear()
        self.api.action.side_effect = CLIError('server unavailable', 503)
        await self.handle('/resume ag_a')
        self.assertEqual(self.output, ['server unavailable'])


class ControllerEventTests(ControllerFixture, unittest.IsolatedAsyncioTestCase):
    async def initialize(self):
        await self.controller.initialize(AsyncMock(return_value='1'))
        self.output.clear()

    def workspace_event(self, **changes):
        ws = copy.deepcopy(self.ws)
        ws['agents'][0].update(changes)
        return {'type': 'workspace', 'data': ws}

    async def test_selected_channel_survives_repeated_settings_omission(self):
        await self.initialize()
        for channels in [['general'], ['general', 'ws-a'], ['general']]:
            self.client.handle_event({'type': 'settings', 'data': {'channels': channels}})
            self.assertEqual(self.client.channel, 'ws-a')
        self.assertFalse(any('using #general' in line for line in self.output))

    async def test_matching_event_updates_state_and_filters_unrelated(self):
        await self.initialize()
        event = self.workspace_event(last_state='running')
        unrelated = copy.deepcopy(event)
        unrelated['data']['id'] = 'ws_other'
        self.client.handle_event(unrelated)
        self.assertEqual(self.output, [])
        self.client.handle_event(event)
        self.assertEqual(self.controller.workspace['agents'][0]['last_state'], 'running')
        self.assertIn('claude-1 running', '\n'.join(self.output))
        count = len(self.output)
        self.client.handle_event(copy.deepcopy(event))
        self.assertEqual(len(self.output), count)

    async def test_pending_done_and_launch_failure_use_cached_http_directory(self):
        await self.initialize()
        self.client.handle_event({'type': 'status', 'data': {'paused': False}})
        self.client.handle_event(self.workspace_event(history_state='pending', last_state='starting'))
        self.assertIn('catching up…', '\n'.join(self.output))
        self.client.handle_event(self.workspace_event(history_state='pending', last_state='exited', last_error='boom'))
        self.assertIn(f'failed to start; see {self.cwd}/logs/wrapper-ag_a.log', '\n'.join(self.output))
        self.assertIn('boom', '\n'.join(self.output))
        self.client.handle_event(self.workspace_event(history_state='done', last_state='running'))
        self.assertIn('history done', self.output[-1])
        self.assertNotIn('summarizing', '\n'.join(self.output))

    async def test_fresh_unknown_id_and_failed_history_note_are_visible_safe(self):
        await self.initialize()
        self.client.handle_event(self.workspace_event(last_state='starting', last_launch={'kind': 'fresh'},
            native_session_id=None, history_state='failed', history_note='history problem\x1b[2J'))
        rendered = '\n'.join(self.output)
        for value in ['fresh', 'id unknown', 'history failed', 'history problem']:
            self.assertIn(value, rendered)
        self.assertNotIn('\x1b', rendered)

    async def test_agents_includes_stopped_members_then_external_live_agents(self):
        await self.initialize()
        self.client.status = {'paused': False, 'claude-1': {'available': False},
                              'external-1': {'available': True, 'role': 'reviewer'}}
        await self.controller.handle('/agents')
        rendered = '\n'.join(self.output)
        for text in ['claude-1 exited', 'claude', self.cwd, 'unread 0', 'id present', '@external-1: online (reviewer)']:
            self.assertIn(text, rendered)
        self.assertLess(rendered.index('claude-1'), rendered.index('external-1'))
        self.assertNotIn('@claude-1:', rendered)

    async def test_agent_line_has_single_unread_and_missing_cwd_presentation(self):
        self.controller.workspace['agents'][0].update(unread_count=3, cwd='/missing/task4-cwd')
        await self.handle('/agents')
        self.assertEqual(len(self.output), 1)
        self.assertEqual(self.output[0].count('unread 3'), 1)
        self.assertEqual(self.output[0].count('⚠ cwd missing'), 1)
        self.assertNotIn('· cwd ', self.output[0])

    async def test_completion_and_help_include_session_commands_and_providers(self):
        self.assertTrue(callable(getattr(self.controller, 'completion_words', None)),
                        'session completion is missing')
        self.controller.providers = ['configured-provider']
        await self.initialize()
        words = self.controller.completion_words()
        for text in ['/spawn', '/resume', '/stop', '/unread', '/retry', '/history', '/rename',
                     '/archive', '/sessions', 'claude-1', 'ag_a', 'claude', 'configured-provider']:
            self.assertIn(text, words)
        self.assertIn('/attach', words)
        await self.controller.handle('/help')
        self.assertIn('/spawn', '\n'.join(self.output))
        self.assertIn('/history AGENT MODE', '\n'.join(self.output))
        for command in ['/jobs', '/rules', '/channels']:
            self.assertIn(command, '\n'.join(self.output))
        self.assertIn('/attach AGENT', '\n'.join(self.output))
        self.assertEqual(self.api.action.call_count, 0)

    async def test_provided_startup_directory_avoids_extra_status_call(self):
        self.assertIn('data_dir', __import__('inspect').signature(chat.WorkspaceChatController).parameters,
                      'startup metadata handoff is missing')
        self.controller = chat.WorkspaceChatController(self.client, self.api, no_resume=True,
            data_dir='/startup/data', providers=['custom'])
        await self.initialize()
        self.api.status.assert_not_called()
        self.client.handle_event(self.workspace_event(last_state='starting'))
        self.client.handle_event(self.workspace_event(last_state='exited', last_error='launch failed'))
        self.assertIn('/startup/data/logs/wrapper-ag_a.log', '\n'.join(self.output))

    async def test_action_matching_prior_event_does_not_duplicate_status(self):
        await self.initialize()
        self.client.handle_event(self.workspace_event(last_state='starting'))
        count = len(self.output)
        await self.handle('/resume ag_a')
        self.assertEqual(len(self.output), count)

    async def test_resume_starting_clears_previous_failed_launch_hint(self):
        await self.initialize()
        self.client.handle_event(self.workspace_event(last_state='starting'))
        self.client.handle_event(self.workspace_event(last_state='exited', last_error='boom'))
        await self.handle('/resume ag_a --fresh')
        self.assertIn('starting', self.output[-1])
        self.assertNotIn('failed to start', self.output[-1])


class ControllerLifecycleTests(ControllerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_quit_defers_checkpoint_to_close_once(self):
        self.assertEqual(await self.handle('/quit'), 'quit')
        self.api.action.assert_not_called()
        await self.controller.close()
        await self.controller.close()
        self.api.action.assert_called_once_with('ws_a', 'checkpoint')

    async def test_switch_checkpoints_before_picker_and_eof_clears_selection(self):
        self.controller.prompt = AsyncMock(side_effect=EOFError)
        self.assertEqual(await self.handle('/sessions'), 'quit')
        self.assertIsNone(self.controller.workspace)
        await self.controller.close()
        self.assertEqual([c[0] for c in self.api.mock_calls], ['action', 'list'])
        self.api.action.assert_called_once_with('ws_a', 'checkpoint')

    async def test_switch_new_selection_changes_channel_and_checkpoints_new_on_close(self):
        other = dict(self.ws, id='ws_b', channel='ws-b', name='other')
        self.api.list.return_value = {'workspaces': [other]}
        self.api.get.side_effect = lambda _: copy.deepcopy(other)
        self.controller.prompt = AsyncMock(return_value='1')
        self.assertEqual(await self.handle('/sessions'), 'continue')
        self.assertEqual(self.controller.workspace['id'], 'ws_b')
        self.assertEqual(self.client.channel, 'ws-b')
        await self.controller.close()
        self.assertEqual([call.args for call in self.api.action.call_args_list],
                         [('ws_a', 'checkpoint'), ('ws_b', 'checkpoint')])

    async def test_archive_default_no_never_mutates(self):
        for answer in ['', 'n', 'N']:
            self.controller.prompt = AsyncMock(return_value=answer)
            self.assertEqual(await self.handle('/archive'), 'continue')
            self.controller.prompt.assert_awaited_once_with('Archive session? [y/N]', default='n')
        self.api.action.assert_not_called()
        self.assertEqual(self.controller.workspace['id'], 'ws_a')

    async def test_archive_yes_uses_server_checkpoint_then_picker_eof_no_repeat(self):
        self.controller.prompt = AsyncMock(side_effect=['Y', EOFError()])
        self.api.action.return_value = dict(self.ws, archived=True)
        self.assertEqual(await self.handle('/archive'), 'quit')
        self.api.action.assert_called_once_with('ws_a', 'archive')
        self.assertIsNone(self.controller.workspace)
        await self.controller.close()
        self.api.action.assert_called_once_with('ws_a', 'archive')

    async def test_checkpoint_failure_warns_once_but_close_finishes(self):
        self.assertTrue(callable(getattr(self.controller, 'close', None)), 'checkpoint close missing')
        for error in [CLIError('specific checkpoint failure\x1b[2J'), OSError('token=SECRET')]:
            self.controller = chat.WorkspaceChatController(self.client, self.api)
            self.controller.workspace = self.ws
            self.api.action.side_effect = error
            await self.controller.close()
            await self.controller.close()
        self.assertEqual(self.api.action.call_count, 2)
        self.assertEqual(len(self.output), 2)
        self.assertIn('Warning:', self.output[0])
        self.assertIn('specific checkpoint failure', self.output[0])
        self.assertNotIn('SECRET', '\n'.join(self.output))
        self.assertNotIn('\x1b', '\n'.join(self.output))

    async def test_nested_prompt_cancellation_does_not_mutate(self):
        for command in ['/spawn claude', f'/spawn claude --cwd {shlex.quote(self.cwd)}', '/archive']:
            for error, expected in [(EOFError(), 'quit'), (KeyboardInterrupt(), 'continue')]:
                with self.subTest(command=command, error=type(error).__name__):
                    self.controller.prompt = AsyncMock(side_effect=error)
                    self.assertEqual(await self.handle(command), expected)
        self.api.action.assert_not_called()

    async def test_interactive_quit_and_eof_checkpoint_before_receiver_shutdown(self):
        for ending in ['/quit', EOFError(), '/spawn claude']:
            with self.subTest(ending=ending):
                events = []
                self.controller = chat.WorkspaceChatController(self.client, self.api,
                    selector='billing', no_resume=True, data_dir=self.cwd)
                self.api.action.reset_mock()
                self.api.action.side_effect = lambda *args, **kwargs: events.append(args[1]) or {'checked': 1}
                started = asyncio.Event()
                async def receiver():
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        events.append('receiver cancelled')
                self.client.receive_forever = receiver
                answers = iter([ending, EOFError()])
                async def prompt(*args, **kwargs):
                    await started.wait()
                    answer = next(answers)
                    if isinstance(answer, BaseException):
                        raise answer
                    return answer
                prompt_session = Mock(prompt_async=prompt)
                with patch('prompt_toolkit.PromptSession', return_value=prompt_session):
                    await cli.interactive(self.client, self.controller)
                self.assertEqual(events, ['checkpoint', 'receiver cancelled'])
                self.api.action.assert_called_once_with('ws_a', 'checkpoint')


class ControllerPollingTests(ControllerFixture, unittest.IsolatedAsyncioTestCase):
    async def poll(self, iterations=1):
        self.assertTrue(callable(getattr(self.controller, 'poll_forever', None)), 'poll fallback missing')
        sleep = AsyncMock(side_effect=[None] * iterations + [asyncio.CancelledError()])
        with patch('cli_workspace_chat.asyncio.sleep', sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.controller.poll_forever()
        self.assertEqual([c.args for c in sleep.await_args_list], [(2,)] * (iterations + 1))

    async def test_connected_running_done_never_refreshes(self):
        self.client.websocket = object()
        self.controller.workspace['agents'][0]['last_state'] = 'running'
        await self.poll(3)
        self.api.get.assert_not_called()
        self.controller.prompt.assert_not_awaited()

    async def test_starting_pending_and_disconnected_independently_refresh(self):
        cases = [('starting', 'done', object()), ('running', 'pending', object()),
                 ('running', 'done', None)]
        for state, history, socket in cases:
            with self.subTest(state=state, history=history, disconnected=socket is None):
                self.controller.workspace = copy.deepcopy(self.ws)
                self.controller.workspace['agents'][0].update(last_state=state, history_state=history)
                self.client.websocket = socket
                self.api.get.reset_mock()
                await self.poll()
                self.api.get.assert_called_once_with('ws_a')
        self.controller.prompt.assert_not_awaited()

    async def test_poll_unchanged_results_do_not_repeat_status_lines(self):
        await self.poll(3)
        self.assertEqual(self.api.get.call_count, 3)
        self.assertEqual(len(self.output), 1)

    async def test_reconnect_settings_refreshes_selected_state_once(self):
        await self.controller.initialize(AsyncMock(return_value='1'))
        self.output.clear()
        self.api.get.reset_mock()
        self.client.websocket = object()
        self.controller.workspace['agents'][0]['last_state'] = 'running'
        refreshed = copy.deepcopy(self.controller.workspace)
        refreshed['agents'][0]['unread_count'] = 4
        self.api.get.side_effect = lambda _: copy.deepcopy(refreshed)
        self.client.handle_event({'type': 'settings', 'data': {'channels': ['general']}})
        self.api.get.assert_not_called()
        self.assertEqual(self.client.channel, 'ws-a')
        await self.poll(3)
        self.api.get.assert_called_once_with('ws_a')
        self.assertEqual(self.controller.workspace['agents'][0]['unread_count'], 4)

    async def test_stale_result_discarded_after_selection_changes_even_back_to_same_id(self):
        self.assertTrue(callable(getattr(self.controller, 'poll_forever', None)), 'poll fallback missing')
        for return_to_same in (False, True):
            with self.subTest(return_to_same=return_to_same):
                entered = threading.Event()
                release = threading.Event()
                old = copy.deepcopy(self.ws)
                def get(_):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('test failed to release HTTP response')
                    return old
                self.api.get.side_effect = get
                self.controller._select(copy.deepcopy(self.ws))
                sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
                with patch('cli_workspace_chat.asyncio.sleep', sleep):
                    task = asyncio.create_task(self.controller.poll_forever())
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        selected = dict(self.ws, id='ws_b', channel='ws-b', name='new selection')
                        self.controller._select(selected)
                        if return_to_same:
                            selected = dict(self.ws, name='selected again')
                            self.controller._select(selected)
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        self.assertEqual(self.controller.workspace, selected)
                    finally:
                        release.set()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_poll_errors_are_sanitized_deduplicated_and_recover(self):
        self.api.get.side_effect = OSError('http://localhost?token=SECRET')
        await self.poll(2)
        self.assertEqual(len(self.output), 1)
        self.assertNotIn('SECRET', self.output[0])
        self.api.get.side_effect = None
        self.api.get.return_value = copy.deepcopy(self.ws)
        await self.poll()
        self.assertIn('claude-1 exited', self.output[-1])

    async def test_failed_reconnect_refresh_retries_until_selected_state_is_current(self):
        await self.controller.initialize(AsyncMock(return_value='1'))
        self.client.websocket = object()
        self.controller.workspace['agents'][0]['last_state'] = 'running'
        refreshed = copy.deepcopy(self.controller.workspace)
        refreshed['agents'][0]['unread_count'] = 9
        self.api.get.reset_mock()
        self.api.get.side_effect = [OSError('offline'), refreshed]
        self.client.handle_event({'type': 'settings', 'data': {'channels': ['general']}})
        await self.poll(3)
        self.assertEqual(self.api.get.call_count, 2)
        self.assertEqual(self.controller.workspace['agents'][0]['unread_count'], 9)

    async def test_interactive_cancels_receiver_and_poll_after_checkpoint(self):
        events = []
        receiver_started = asyncio.Event()
        poll_started = asyncio.Event()
        async def background(started, label):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append(label)
        self.client.receive_forever = lambda: background(receiver_started, 'receiver cancelled')
        self.controller.poll_forever = lambda: background(poll_started, 'poll cancelled')
        self.controller.selector = 'billing'
        self.api.action.side_effect = lambda *args, **kwargs: events.append('checkpoint') or {'checked': 1}
        async def prompt(*args, **kwargs):
            await receiver_started.wait()
            await poll_started.wait()
            raise EOFError
        with patch('prompt_toolkit.PromptSession', return_value=Mock(prompt_async=prompt)):
            try:
                async with asyncio.timeout(.5):
                    await cli.interactive(self.client, self.controller)
            except TimeoutError:
                self.fail('interactive did not start and own both background tasks')
        self.assertEqual(events[0], 'checkpoint')
        self.assertCountEqual(events[1:], ['receiver cancelled', 'poll cancelled'])


class ControllerSnapshotOrderingTests(ControllerFixture, unittest.IsolatedAsyncioTestCase):
    async def initialize(self):
        await self.controller.initialize(AsyncMock(return_value='1'))
        self.client.websocket = object()
        self.api.get.reset_mock()
        self.output.clear()

    async def delayed_response(self, method, result, operation, meanwhile):
        entered = threading.Event()
        release = threading.Event()
        def respond(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('HTTP response was not released')
            return result
        method.side_effect = respond
        task = asyncio.create_task(operation())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await meanwhile()
            release.set()
            return await task
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def poll_once(self):
        with patch('cli_workspace_chat.asyncio.sleep',
                   AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):
                await self.controller.poll_forever()

    async def test_delayed_poll_preserves_newer_event_and_reconciles_again(self):
        await self.initialize()
        old = copy.deepcopy(self.ws)
        old['agents'][0]['last_state'] = 'running'
        newer = copy.deepcopy(self.ws)
        self.controller.on_settings({})
        async def event():
            self.client.handle_event({'type': 'workspace', 'data': newer})
        await self.delayed_response(self.api.get, old, self.poll_once, event)
        self.assertEqual(self.controller.workspace['agents'][0]['last_state'], 'exited')
        self.assertTrue(self.controller._refresh_requested)
        self.api.get.side_effect = None
        self.api.get.return_value = copy.deepcopy(newer)
        await self.poll_once()
        self.assertEqual(self.api.get.call_count, 2)
        self.assertFalse(self.controller._refresh_requested)

    async def test_delayed_agent_mutations_preserve_newer_events_without_replay(self):
        cases = ['/resume ag_a', '/stop ag_a', '/history ag_a none',
                 f'/spawn codex --cwd {shlex.quote(self.cwd)} --history-mode none']
        for command in cases:
            with self.subTest(command=command):
                await self.initialize()
                self.api.action.reset_mock()
                result = dict(self.agent, last_state='running')
                if command.startswith('/spawn'):
                    result.update(agent_id='ag_new', registry_name='codex-1', provider='codex')
                newer = copy.deepcopy(self.ws)
                newest_agent = dict(result, last_state='exited', unread_count=7)
                newer['agents'] = [newest_agent]
                async def event():
                    self.client.handle_event({'type': 'workspace', 'data': newer})
                await self.delayed_response(self.api.action, result,
                                           lambda: self.handle(command), event)
                self.assertEqual(self.controller.workspace, newer)
                self.assertTrue(self.controller._refresh_requested)
                self.api.get.side_effect = None
                self.api.get.return_value = copy.deepcopy(newer)
                await self.poll_once()
                self.api.action.assert_called_once()
                self.api.get.assert_called_once_with('ws_a')
                self.assertEqual(self.controller.workspace, newer)

    async def test_delayed_rename_preserves_newer_workspace_and_reconciles(self):
        await self.initialize()
        result = dict(self.ws, name='requested name')
        newer = copy.deepcopy(self.ws)
        newer['name'] = 'later rename'
        newer['agents'][0]['last_state'] = 'running'
        async def event():
            self.client.handle_event({'type': 'workspace', 'data': newer})
        await self.delayed_response(self.api.rename, result,
                                   lambda: self.handle('/rename "requested name"'), event)
        self.assertEqual(self.controller.workspace, newer)
        self.assertTrue(self.controller._refresh_requested)
        self.api.get.side_effect = None
        self.api.get.return_value = copy.deepcopy(newer)
        await self.poll_once()
        self.api.rename.assert_called_once_with('ws_a', 'requested name')
        self.api.get.assert_called_once_with('ws_a')

    async def test_command_state_update_supersedes_inflight_poll(self):
        await self.initialize()
        old = copy.deepcopy(self.ws)
        old['agents'][0]['last_state'] = 'running'
        self.controller.on_settings({})
        async def stop():
            self.api.action.return_value = copy.deepcopy(self.agent)
            await self.handle('/stop ag_a')
        await self.delayed_response(self.api.get, old, self.poll_once, stop)
        self.assertEqual(self.controller.workspace['agents'][0]['last_state'], 'exited')
        self.assertTrue(self.controller._refresh_requested)
        self.api.action.assert_called_once_with('ws_a', 'stop', 'ag_a')


class OutputBufferTests(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.client = ChatClient('http://localhost:8300', output=self.output.append)
        self.client.ready.set()
        self.assertTrue(callable(getattr(self.client, 'pause_output', None)), 'pause_output missing')

    def test_buffer_keeps_cache_live_and_flushes_once(self):
        self.client.pause_output()
        self.client.handle_event({'type': 'message', 'data': {
            'id': 7, 'channel': 'general', 'sender': 'human', 'text': 'during attach\x1b\x00'}})
        self.assertIn(7, self.client.messages)
        self.assertEqual(self.output, [])
        self.client.resume_output()
        self.assertIn('during attach', '\n'.join(self.output))
        self.assertNotIn('\x1b', '\n'.join(self.output))
        self.assertNotIn('\x00', '\n'.join(self.output))
        original = list(self.output)
        self.client.resume_output()
        self.assertEqual(self.output, original)

    def test_multiline_buffer_bound_preserves_latest_lines_and_notice(self):
        self.client.pause_output()
        self.client.show('\n'.join(f'line {n}' for n in range(10003)))
        self.assertEqual(self.output, [])
        self.client.resume_output()
        self.assertIn('3', self.output[0])
        self.assertIn('omitted', self.output[0])
        self.assertEqual(self.output[1:], [f'line {n}' for n in range(3, 10003)])
        self.client.pause_output()
        self.client.show('fresh')
        self.client.resume_output()
        self.assertEqual(self.output[-1], 'fresh')
        self.assertEqual(sum('omitted' in line for line in self.output), 1)

    def test_immediate_menu_is_sanitized_while_events_wait(self):
        self.client.pause_output()
        self.client.show('later')
        self.client.show('Sessions\x1b\x00', immediate=True)
        self.assertEqual(self.output, ['Sessions'])
        self.client.resume_output()
        self.assertEqual(self.output, ['Sessions', 'later'])


class ControlledSocket:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.processed = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def __aiter__(self):
        while True:
            frame = await self.frames.get()
            yield json.dumps(frame)
            self.processed.put_nowait(None)

    async def deliver(self, frame):
        await self.frames.put(frame)
        await asyncio.wait_for(self.processed.get(), 2)


class AttachControllerTests(ControllerFixture, unittest.IsolatedAsyncioTestCase):
    async def initialize(self):
        await self.controller.initialize(AsyncMock(return_value='1'))
        self.client.ready.set()
        self.output.clear()

    async def test_attach_receiver_survives_and_buffers_messages_and_state(self):
        await self.initialize()
        self.assertIn('/attach', self.controller.completion_words())
        await self.handle('/help')
        self.assertIn('/attach AGENT', self.output[-1])
        self.output.clear()
        socket = ControlledSocket()
        entered, release = threading.Event(), threading.Event()
        def runner(argv, **kwargs):
            if argv[1] == 'has-session':
                return subprocess.CompletedProcess(argv, 0)
            entered.set()
            if not release.wait(3):
                raise AssertionError('attach not released')
            return subprocess.CompletedProcess(argv, 0)
        real_attach = __import__('cli_workspaces').attach_agent
        def attach(agent, **kwargs):
            return real_attach(agent, runner=runner, **kwargs)
        with patch('websockets.asyncio.client.connect', return_value=socket), \
                patch('cli.fetch_session_token', return_value='secret'), \
                patch.dict('os.environ', {'TMUX': 'inside'}), \
                patch.object(chat, 'attach_agent', side_effect=attach, create=True):
            receiver = asyncio.create_task(self.client.receive_forever())
            await socket.deliver({'type': 'history_complete'})
            self.output.clear()
            task = asyncio.create_task(self.handle('/attach claude-1'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await socket.deliver({'type': 'message', 'data': {
                    'id': 7, 'channel': 'ws-a', 'sender': 'human', 'text': 'during attach'}})
                newer = copy.deepcopy(self.ws)
                newer['agents'][0]['last_state'] = 'running'
                await socket.deliver({'type': 'workspace', 'data': newer})
                self.assertIn(7, self.client.messages)
                self.assertEqual(self.controller.workspace['agents'][0]['last_state'], 'running')
                self.assertEqual(self.output, [])
                self.assertFalse(receiver.done())
                self.assertFalse(task.done())
                release.set()
                self.assertEqual(await task, 'continue')
                self.assertEqual(sum('during attach' in line for line in self.output), 1)
                self.assertIn('Switch back: tmux switch-client -l', self.output)
                self.assertFalse(receiver.done())
                before = list(self.output)
                self.client.resume_output()
                self.assertEqual(self.output, before)
            finally:
                release.set()
                task.cancel()
                receiver.cancel()
                await asyncio.gather(task, receiver, return_exceptions=True)

    async def test_attach_failures_restore_output(self):
        await self.initialize()
        for error in (CLIError('not running; resume with /resume claude-1'),
                      OSError('secret-token')):
            with self.subTest(error=type(error).__name__), patch.object(
                    chat, 'attach_agent', side_effect=error, create=True):
                self.assertEqual(await self.handle('/attach ag_a'), 'continue')
                self.client.show('prompt restored')
                self.assertEqual(self.output[-1], 'prompt restored')
                self.assertNotIn('secret-token', '\n'.join(self.output))
        self.assertIn('not running; resume with /resume claude-1', self.output)

    async def test_attach_cancellation_restores_output(self):
        await self.initialize()
        entered, release = threading.Event(), threading.Event()
        def attach(*args, **kwargs):
            entered.set()
            release.wait(3)
        with patch.object(chat, 'attach_agent', side_effect=attach, create=True):
            task = asyncio.create_task(self.handle('/attach ag_a'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.client.show('queued')
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.output, ['queued'])
                self.client.show('restored')
                self.assertEqual(self.output[-1], 'restored')
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_attach_invalid_plain_windows_and_ambiguous_inputs(self):
        await self.initialize()
        with patch.object(chat, 'attach_agent', create=True) as attach:
            for command in ('/attach', '/attach ag_a extra', '/attach unknown'):
                self.assertEqual(await self.handle(command), 'continue')
            self.controller.workspace['agents'].append(dict(self.agent, agent_id='ag_b', registry_name='claude-2'))
            await self.handle('/attach claude')
            self.assertIn('ambiguous agent', self.output[-1])
            with patch('cli_workspaces.os.name', 'nt'):
                await self.handle('/attach ag_a')
            self.assertIn('Requires tmux', self.output[-1])
            self.controller.plain_channel = True
            await self.handle('/attach ag_a')
            self.assertIn('requires a selected session', self.output[-1])
            attach.assert_not_called()

    async def test_midchat_picker_keeps_menu_visible_and_buffers_receiver(self):
        await self.initialize()
        socket = ControlledSocket()
        entered, release = asyncio.Event(), asyncio.Event()
        async def prompt(*args, **kwargs):
            entered.set()
            await release.wait()
            return '1'
        self.controller.prompt = prompt
        with patch('websockets.asyncio.client.connect', return_value=socket), \
                patch('cli.fetch_session_token', return_value='secret'):
            receiver = asyncio.create_task(self.client.receive_forever())
            await socket.deliver({'type': 'history_complete'})
            self.output.clear()
            picker = asyncio.create_task(self.handle('/sessions'))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertIn('Sessions', self.output)
                menu = list(self.output)
                await socket.deliver({'type': 'message', 'data': {
                    'id': 8, 'channel': 'ws-a', 'sender': 'human', 'text': 'during picker'}})
                self.assertIn(8, self.client.messages)
                self.assertEqual(self.output, menu)
                self.assertFalse(receiver.done())
                release.set()
                self.assertEqual(await picker, 'continue')
                self.assertIn('during picker', '\n'.join(self.output))
                self.client.show('restored')
                self.assertEqual(self.output[-1], 'restored')
            finally:
                release.set()
                picker.cancel()
                receiver.cancel()
                await asyncio.gather(picker, receiver, return_exceptions=True)

    async def test_picker_cancellation_restores_output(self):
        await self.initialize()
        for error in (EOFError(), KeyboardInterrupt(), asyncio.CancelledError()):
            self.controller._select(copy.deepcopy(self.ws))
            self.controller.prompt = AsyncMock(side_effect=error)
            try:
                await self.handle('/sessions')
            except asyncio.CancelledError:
                pass
            self.client.show('restored')
            self.assertEqual(self.output[-1], 'restored')

    async def test_initial_and_midchat_selection_reconcile_once_after_picker(self):
        self.client.websocket = object()
        self.controller.no_resume = False
        for initial in (True, False):
            with self.subTest(initial=initial):
                self.api.get.reset_mock()
                self.api.get.side_effect = lambda _: copy.deepcopy(self.ws)
                async def prompt(text, **kwargs):
                    if text == 'Choose:':
                        return '1'
                    # Event lands after picker fetch, during resume question.
                    newer = copy.deepcopy(self.ws)
                    newer['agents'][0].update(unread_count=9, last_state='running')
                    self.client.handle_event({'type': 'workspace', 'data': newer})
                    self.api.get.side_effect = lambda _: copy.deepcopy(newer)
                    return 'n'
                if initial:
                    await self.controller.initialize(prompt)
                else:
                    self.controller.prompt = prompt
                    await self.handle('/sessions')
                self.assertEqual(self.controller.workspace['agents'][0]['unread_count'], 0)
                self.assertTrue(self.controller._refresh_requested)
                self.api.get.reset_mock()
                with patch('cli_workspace_chat.asyncio.sleep', AsyncMock(
                        side_effect=[None, None, asyncio.CancelledError()])):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.controller.poll_forever()
                self.api.get.assert_called_once_with('ws_a')
                self.assertEqual(self.controller.workspace['agents'][0]['unread_count'], 9)


if __name__ == '__main__':
    unittest.main()
