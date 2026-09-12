"""Real HTTP/chat/tmux session workflows, using only an inert kilo shim."""

import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from _cli_server import (IsolatedCliServer, cli, isolated_environment,
                         log_excerpt, temporary_ports)
from cli_api import CLIError
from cli_workspace_chat import WorkspaceChatController, ensure_server
from config_loader import load_config

SUMMARY_ERROR = 'summary history mode is not available in this version; use literal or none'


class WorkspaceShellIntegrationTests(IsolatedCliServer):
    def test_session_send_read_and_plain_channel_regression(self):
        created = self.command('new', 'billing cli', '--json')
        self.assertEqual(created.returncode, 0, created.stderr)
        ws = json.loads(created.stdout)
        sent = self.command('send', '--session', ws['id'], '--json', 'hello from session cli')
        self.assertEqual(sent.returncode, 0, sent.stderr)
        read = self.command('read', '--session', ws['id'], '--json')
        self.assertIn(json.loads(sent.stdout), json.loads(read.stdout))
        self.assertEqual(json.loads(sent.stdout)['channel'], ws['channel'])
        plain = self.json_command('send', 'plain channel regression')
        self.assertEqual(plain['channel'], 'general')
        self.assertIn(plain, self.json_command('read'))
        self.assertNotIn(plain, self.json_command('read', '--session', ws['id']))

    def test_names_prefixes_ambiguity_and_archived_listing(self):
        first = self.json_command('new', 'resolution billing')
        second = self.json_command('new', 'resolution batch')
        for selector in (first['id'], first['name'], 'resolution bill'):
            sent = self.json_command('send', '--session', selector, 'resolved')
            self.assertEqual(sent['channel'], first['channel'])
        ambiguous = self.command('read', '--session', 'resolution b', '--json')
        self.assertEqual(ambiguous.returncode, 1)
        self.assertEqual(ambiguous.stdout, '')
        for ws in (first, second):
            self.assertIn(ws['id'], ambiguous.stderr)
            self.assertIn(ws['name'], ambiguous.stderr)
        duplicate = self.json_command('new', first['name'])
        ambiguous = self.command('read', '--session', first['name'], '--json')
        self.assertEqual(ambiguous.returncode, 1)
        self.assertIn(duplicate['id'], ambiguous.stderr)
        refused = self.command('archive', first['id'], '--json', input='')
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(refused.stdout, '')
        self.assertFalse(self.api.get(first['id'])['archived'])
        archived = self.json_command('archive', first['id'], '--yes')
        self.assertTrue(archived['archived'])
        active = self.json_command('sessions')['workspaces']
        all_rows = self.json_command('sessions', '--archived')['workspaces']
        self.assertNotIn(first['id'], [ws['id'] for ws in active])
        self.assertIn(first['id'], [ws['id'] for ws in all_rows])
        self.assertIn(second['id'], [ws['id'] for ws in all_rows])
        self.assertTrue(self.api.get(first['id'])['archived'])

    def test_summary_refusal_and_unknown_agents_have_clean_json_stdout(self):
        ws = self.json_command('new', 'policy cli')
        result = self.command('spawn', 'kilo', '--session', ws['id'], '--cwd', self.temp.name,
                              '--history-mode', 'summary', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn(SUMMARY_ERROR, result.stderr)
        self.assertEqual(self.api.get(ws['id'])['agents'], [])
        self.assertEqual(self.json_command('unread', '--session', ws['id']), {'agents': []})
        for command in ('stop', 'retry', 'unread'):
            with self.subTest(command=command):
                agent_args = ('--agent', 'missing-agent') if command == 'unread' else ('missing-agent',)
                result = self.command(command, *agent_args, '--session', ws['id'], '--json')
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertIn('missing-agent', result.stderr)


class WorkspacePromptIntegrationTests(IsolatedCliServer):
    def test_empty_picker_chat_rename_switch_and_quit_checkpoint(self):
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.input import create_pipe_input
            from prompt_toolkit.output import DummyOutput
        except ImportError:
            self.skipTest('Install requirements-cli.txt for interactive prompt coverage')

        async def scenario():
            self.assertEqual(self.api.list()['workspaces'], [])
            output, prompts, checkpoints = [], [], []
            client = cli.ChatClient(self.url, output=output.append)
            controller = WorkspaceChatController(client, self.api, no_resume=True)
            original_action = self.api.action

            def action(ws_id, operation, *args, **kwargs):
                result = original_action(ws_id, operation, *args, **kwargs)
                if operation == 'checkpoint':
                    checkpoints.append((ws_id, result))
                return result

            async def until(predicate):
                while not predicate():
                    await asyncio.sleep(.02)

            with create_pipe_input() as pipe:
                def session(**kwargs):
                    prompt_session = PromptSession(input=pipe, output=DummyOutput(), **kwargs)
                    original_prompt = prompt_session.prompt_async

                    async def prompt(*args, **kwargs):
                        prompts.append(args[0] if args else '')
                        return await original_prompt(*args, **kwargs)

                    prompt_session.prompt_async = prompt
                    return prompt_session

                with patch('prompt_toolkit.PromptSession', side_effect=session), \
                        patch.object(self.api, 'action', side_effect=action):
                    task = asyncio.create_task(cli.interactive(client, controller))
                    try:
                        async with asyncio.timeout(20):
                            await until(lambda: 'Session name:' in prompts)
                            pipe.send_text('prompt billing\n')
                            await client.ready.wait()
                            first = controller.workspace['id']
                            pipe.send_text('selected session message\n')
                            await until(lambda: any('selected session message' in line for line in output))
                            self.assertEqual(client.channel, controller.workspace['channel'])
                            pipe.send_text('/rename "prompt renamed"\n')
                            await until(lambda: controller.workspace['name'] == 'prompt renamed')
                            pipe.send_text('/sessions\n')
                            await until(lambda: 'Choose:' in prompts)
                            self.assertEqual(checkpoints, [(first, {'checked': 0})])
                            pipe.send_text('n\n')
                            await until(lambda: prompts.count('Session name:') == 2)
                            pipe.send_text('prompt second\n')
                            await until(lambda: controller.workspace is not None
                                        and controller.workspace['name'] == 'prompt second')
                            second = controller.workspace['id']
                            pipe.send_text('/quit\n')
                            await task
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(checkpoints, [(first, {'checked': 0}), (second, {'checked': 0})])
            self.assertIsNone(client.websocket)
            self.assertEqual(self.api.get(first)['name'], 'prompt renamed')
            self.assertFalse(self.api.get(first)['archived'])
            messages = self.json_command('read', '--session', first)
            self.assertIn('selected session message', [m['text'] for m in messages])
        asyncio.run(scenario())


@unittest.skipUnless(shutil.which('tmux') and sys.platform != 'win32', 'Requires Unix tmux')
class WorkspaceTmuxIntegrationTests(IsolatedCliServer):
    @classmethod
    def environment_additions(cls):
        shim_dir = Path(cls.temp.name) / 'bin'
        shim_dir.mkdir()
        shim = shim_dir / 'kilo'
        shim.write_text(f'#!{sys.executable}\nimport sys\nprint("KILO TEST READY", flush=True)\n'
                        'for line in sys.stdin:\n    print("KILO TEST RECEIVED", flush=True)\n')
        shim.chmod(0o755)
        return {'PATH': str(shim_dir) + os.pathsep + os.environ['PATH']}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(cls.kill_isolated_tmux)

    @classmethod
    def kill_isolated_tmux(cls):
        subprocess.run(['tmux', 'kill-server'], env=cls.env, capture_output=True, timeout=5)

    def tmux_exists(self, name):
        return subprocess.run(['tmux', 'has-session', '-t', '=' + name], env=self.env,
                              capture_output=True, timeout=5).returncode == 0

    def test_real_cli_stub_spawn_stop_retry_fresh_resume_and_archive(self):
        ws = self.json_command('new', 'stub lifecycle')
        self.addCleanup(self.api.action, ws['id'], 'archive')
        agent = self.json_command('spawn', 'kilo', '--session', ws['id'], '--cwd', self.temp.name,
                                  '--agent-name', 'stub-reviewer', '--history-mode', 'none')
        agent_id = agent['agent_id']
        def current():
            return next(item for item in self.api.get(ws['id'])['agents'] if item['agent_id'] == agent_id)
        self.poll(lambda: current()['last_state'] == 'running')
        target = current().get('tmux_session') or 'agentchattr-' + agent_id
        self.assertTrue(self.tmux_exists(target))
        no_unread = self.command('retry', 'stub-reviewer', '--session', ws['id'], '--json')
        self.assertEqual(no_unread.returncode, 1)
        self.assertEqual(no_unread.stdout, '')
        self.assertIn('unread', no_unread.stderr.lower())
        sent = self.json_command('send', '--session', ws['id'], '@stub-reviewer queued work')
        unread = self.json_command('unread', '--agent', 'stub-reviewer', '--session', ws['id'])
        row = next(row for row in unread['agents'] if row['agent_id'] == agent_id)
        self.assertEqual(row['count'], 1)
        self.assertIn(sent['id'], [message['id'] for message in row['messages']])
        self.assertEqual(self.json_command('retry', 'stub-reviewer', '--session', ws['id']), {'ok': True})
        stopped = self.json_command('stop', agent_id, '--session', ws['id'])
        self.assertEqual(stopped['last_state'], 'exited')
        self.poll(lambda: not self.tmux_exists(target))
        refused = self.command('resume', 'stub-reviewer', '--session', ws['id'], '--json')
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(refused.stdout, '')
        self.assertIn('--fresh', refused.stderr)
        resumed = self.json_command('resume', 'stub-reviewer', '--session', ws['id'], '--fresh')
        self.assertEqual(resumed['agent_id'], agent_id)
        self.assertEqual(resumed['last_launch']['kind'], 'fresh')
        self.poll(lambda: current()['last_state'] == 'running')
        self.assertTrue(self.tmux_exists(target))
        archived = self.json_command('archive', ws['id'], '--yes')
        self.assertTrue(archived['archived'])
        self.poll(lambda: current()['last_state'] == 'exited' and not self.tmux_exists(target))


class LogExcerptTests(unittest.TestCase):
    def test_token_straddling_excerpt_boundary_is_redacted_before_slicing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server.log'
            entry = 'GET /ws?token=boundary-secret&channel=general\n'
            log = entry + 'x' * (12000 - len(entry[9:]))
            self.assertTrue(log[-12000:].startswith('oken=boundary-secret'))
            path.write_text(log)
            excerpt = log_excerpt(path)
        self.assertFalse('boundary-secret' in excerpt, 'boundary token must be redacted')
        self.assertIn('[REDACTED]', excerpt)
        self.assertLessEqual(len(excerpt), 12000)


class ServerStartupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agentchattr-cli-start-')
        self.addCleanup(self.temp.cleanup)
        self.env = isolated_environment(self.temp.name)
        self.ports = temporary_ports()
        self.url = f'http://127.0.0.1:{self.ports[0]}'
        self.data = Path(self.temp.name) / 'changed data'
        self.uploads = Path(self.temp.name) / 'changed uploads'
        self.log_path = self.data / 'logs/server.log'

    def tearDown(self):
        result = self._outcome.result
        if any(test is self or getattr(test, 'test_case', None) is self
               for test, _ in result.failures + result.errors):
            sys.stderr.write('\nAuto-start server log:\n' + log_excerpt(self.log_path) + '\n')

    def test_explicit_url_down_never_calls_tmux(self):
        with patch.dict(os.environ, self.env, clear=True), \
                patch('cli_workspace_chat.subprocess.run') as run:
            with self.assertRaises(CLIError) as caught:
                ensure_server(self.url, explicit_url=True,
                              config={'server': {'data_dir': str(self.data)}})
        run.assert_not_called()
        self.assertIn('Start it manually: python run.py', str(caught.exception))
        self.assertIn(str(self.log_path), str(caught.exception))

    def test_failure_log_redacts_query_and_startup_tokens(self):
        self.log_path.parent.mkdir(parents=True)
        self.log_path.write_text('GET /ws?token=query-secret&channel=general\n'
                                 'GET /ws?x=1&token=second-secret" 101\n'
                                 'Session token: startup-secret\n')
        excerpt = log_excerpt(self.log_path)
        for secret in ('query-secret', 'second-secret', 'startup-secret'):
            self.assertNotIn(secret, excerpt)
        self.assertEqual(excerpt.count('[REDACTED]'), 3)
        self.assertIn('channel=general', excerpt)

    @unittest.skipUnless(shutil.which('tmux') and sys.platform != 'win32', 'Requires Unix tmux')
    def test_autostart_uses_five_flags_with_preexisting_tmux_environment(self):
        stale_ports = temporary_ports()
        stale = dict(self.env, AGENTCHATTR_PORT=str(stale_ports[0]),
                     AGENTCHATTR_MCP_HTTP_PORT=str(stale_ports[1]),
                     AGENTCHATTR_MCP_SSE_PORT=str(stale_ports[2]),
                     AGENTCHATTR_DATA_DIR=str(Path(self.temp.name) / 'stale data'),
                     AGENTCHATTR_UPLOAD_DIR=str(Path(self.temp.name) / 'stale uploads'))
        # Register cleanup before launching or waiting. TMUX was removed above.
        self.addCleanup(subprocess.run, ['tmux', 'kill-server'], env=stale,
                        capture_output=True, timeout=5)
        result = subprocess.run(['tmux', 'new-session', '-d', '-s', 'fixture-keeper',
                                 'exec /bin/sleep 120'], env=stale, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        changed = dict(self.env, AGENTCHATTR_PORT=str(self.ports[0]),
                       AGENTCHATTR_MCP_HTTP_PORT=str(self.ports[1]),
                       AGENTCHATTR_MCP_SSE_PORT=str(self.ports[2]),
                       AGENTCHATTR_DATA_DIR=str(self.data), AGENTCHATTR_UPLOAD_DIR=str(self.uploads))
        output = []
        with patch.dict(os.environ, changed, clear=True):
            config = load_config()
            with patch('cli_workspace_chat.subprocess.run', wraps=subprocess.run) as run:
                status = ensure_server(self.url, explicit_url=False, config=config, output=output.append)
            self.assertEqual(Path(status['data_dir']), self.data)
            self.assertIn('Started server in tmux session agentchattr-server.', output)
            launches = [call.args[0] for call in run.call_args_list
                        if call.args[0][:2] == ['tmux', 'new-session']]
            self.assertEqual(len(launches), 1)
            words = shlex.split(launches[0][-1])
            for flag, value in [('--port', self.ports[0]), ('--mcp-http-port', self.ports[1]),
                                ('--mcp-sse-port', self.ports[2]), ('--data-dir', self.data),
                                ('--upload-dir', self.uploads)]:
                self.assertIn(flag, words)
                self.assertEqual(words[words.index(flag) + 1], str(value))
            for port in self.ports[1:]:
                deadline = monotonic() + 5
                while (remaining := deadline - monotonic()) > 0:
                    try:
                        with socket.create_connection(('127.0.0.1', port),
                                                      timeout=min(.25, remaining)):
                            break
                    except OSError:
                        sleep(min(.05, max(0, deadline - monotonic())))
                else:
                    self.fail(f'MCP listener on port {port} unavailable after 5 seconds')
            # Upload directories are created lazily: exercise the configured path.
            svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
            body = (b'--cli-boundary\r\nContent-Disposition: form-data; name="file"; '
                    b'filename="test.svg"\r\nContent-Type: image/svg+xml\r\n\r\n' + svg +
                    b'\r\n--cli-boundary--\r\n')
            request = Request(self.url + '/api/upload', data=body, headers={
                'Content-Type': 'multipart/form-data; boundary=cli-boundary',
                'X-Session-Token': cli.fetch_session_token(self.url)})
            with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                uploaded = json.load(response)
            self.assertEqual((self.uploads / Path(uploaded['url']).name).read_bytes(), svg)
            self.assertTrue(self.log_path.is_file())
            config['server']['data_dir'] = str(Path(self.temp.name) / 'mismatch')
            same = ensure_server(self.url, explicit_url=True, config=config, output=output.append)
            self.assertEqual(same['data_dir'], status['data_dir'])
            self.assertIn(f'Warning: server data_dir {self.data} differs from configured '
                          f'{Path(self.temp.name) / "mismatch"}', output)


if __name__ == '__main__':
    unittest.main()
