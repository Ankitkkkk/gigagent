"""Interactive startup and session selection boundary regressions."""

import asyncio
import builtins
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
            subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]).start()
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

    def test_launch_failure_has_manual_log_tmux_hint(self):
        self.down()
        self.runner.side_effect = [subprocess.CompletedProcess([], 1),
                                   subprocess.CompletedProcess([], 1)]
        with self.assertRaises(CLIError) as error:
            self.ensure()
        self.assert_manual(error)
        self.assertIn('agentchattr-server', str(error.exception))
        self.assertNotIn('Started server in tmux session agentchattr-server.', self.output)

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
                raise CLIError('native session id unknown', 409)
            agent['last_state'] = 'starting'
            return dict(agent)
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
            ('POST', '/api/workspaces/ws_abcd00/agents/ag_a/resume', {'cwd': str(self.tmp_path)}),
            ('POST', '/api/workspaces/ws_abcd00/agents/ag_b/resume', {'cwd': str(self.tmp_path)})])
        self.assertEqual(self.calls[-1], ('GET', '/api/workspaces/ws_abcd00', None))
        self.assertEqual(selected['agents'][0]['last_state'], 'starting')

    async def test_resume_refusal_prints_exact_fresh_hint_and_continues(self):
        self.workspaces = [self.workspace(agents=[self.agent(),
            self.agent(agent_id='ag_b', registry_name='codex-1')])]
        self.refuse.add('ag_a')
        await self.choose('1', 'yes')
        self.assertIn('native session id unknown', self.output)
        self.assertIn('/resume claude-1 --fresh', self.output)
        self.assertEqual([c[1] for c in self.calls if c[0] == 'POST'], [
            '/api/workspaces/ws_abcd00/agents/ag_a/resume', '/api/workspaces/ws_abcd00/agents/ag_b/resume'])

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


if __name__ == '__main__':
    unittest.main()
