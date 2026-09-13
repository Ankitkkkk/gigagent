"""Real-server TUI acceptance; no paid provider or developer tmux socket."""

import asyncio
import copy
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests._cli_server import ROOT, IsolatedCliServer, cli
from tests._tui_harness import PTY_OBSERVER, PtyTerminal, application_harness
from cli_workspace_chat import WorkspaceChatController


class TuiIntegrationTests(IsolatedCliServer, unittest.IsolatedAsyncioTestCase):
    """Catches lost/duplicate sends and missing switch/Quit checkpoints."""

    @asynccontextmanager
    async def real_tui(self):
        client = cli.ChatClient(self.url)
        controller = WorkspaceChatController(client, self.api, no_resume=True,
                                             data_dir=str(self.data_dir))
        events = []
        original_action = self.api.action
        original_receive = client.receive_forever

        def action(ident, operation, *args, **kwargs):
            result = original_action(ident, operation, *args, **kwargs)
            if operation == 'checkpoint':
                events.append(('checkpoint', ident))
            return result

        async def receive():
            events.append(('receiver_start',))
            try:
                await original_receive()
            finally:
                events.append(('receiver_cancel',))

        with patch.object(self.api, 'action', side_effect=action), \
                patch.object(client, 'receive_forever', side_effect=receive):
            async with application_harness(client, controller, size=(120, 30)) as ui:
                ui.events = events
                await ui.wait_until(lambda: ui.dialogs.future is not None
                                    and 'Show archived' in ui.screen_text(), timeout=15)
                yield ui

    async def create_session(self, ui, name):
        await ui.activate_named('new_session')
        await ui.wait_until(lambda: 'Session name:' in ui.screen_text())
        await ui.type_text(name)
        await ui.key('Tab')
        await ui.key('Enter')
        await ui.wait_until(lambda: ui.controller.workspace is not None
                            and ui.controller.workspace['name'] == name, timeout=15)
        await asyncio.wait_for(ui.client.ready.wait(), 15)
        await ui.wait_until(lambda: ui.focused_control == 'composer')
        return ui.controller.workspace['id']

    async def test_real_session_message_switch_and_quit(self):
        async with self.real_tui() as ui:
            first = await self.create_session(ui, 'billing')
            text = 'Please review retries'
            await ui.type_text(text)
            await ui.key('Left')
            cursor = ui.view.composer.buffer.cursor_position
            snapshot = copy.deepcopy(await asyncio.to_thread(self.api.get, first))
            # Synthetic server-shaped status only; messages use real WebSocket.
            snapshot['agents'] = [dict(
                agent_id='ag_synthetic_status', registry_name='qa-status', provider='kilo',
                cwd=self.temp.name, last_state='running', native_session_id='NATIVE-SECRET-QA',
                tmux_session='never-launch-synthetic', history_mode='none', history_state='done',
                history_note=None, unread_count=0, last_error=None, last_launch=None)]
            ui.client.handle_event({'type': 'workspace', 'data': snapshot})
            await ui.wait_until(lambda: 'qa-status' in ui.screen_text())
            self.assertEqual(ui.view.composer.text, text)
            self.assertEqual(ui.view.composer.buffer.cursor_position, cursor)
            self.assertNotIn('NATIVE-SECRET-QA', ui.screen_text())
            self.assertEqual(ui.client.messages, {})
            await ui.key('Enter')
            await ui.wait_until(lambda: any(m.get('text') == text
                                           for m in ui.client.messages.values()), timeout=15)
            await ui.wait_until(lambda: text in ui.screen_text())
            self.assertEqual(ui.screen_text().count(text), 1)
            self.assertEqual(ui.view.composer.text, '')
            await ui.key('F2')
            await ui.wait_until(lambda: 'Show archived' in ui.screen_text())
            await ui.key('Escape')
            self.assertFalse(any(event[0] == 'checkpoint' for event in ui.events))
            await ui.key('F2')
            second = await self.create_session(ui, 'frontend')
            self.assertEqual([e[1] for e in ui.events if e[0] == 'checkpoint'], [first])
            receiver, poller = ui.tui.receiver_task, ui.tui.poller_task
            await ui.key('CtrlQ')
            self.assertIsNone(await asyncio.wait_for(ui.task, 15))
            self.assertEqual([e[1] for e in ui.events if e[0] == 'checkpoint'], [first, second])
            self.assertLess(ui.events.index(('checkpoint', second)),
                            ui.events.index(('receiver_cancel',)))
            self.assertEqual(ui.events.count(('receiver_start',)), 1)
            self.assertTrue(receiver.done() and poller.done())
            self.assertIsNone(ui.client.websocket)
        messages = await asyncio.to_thread(self.json_command, 'read', '--session', first)
        self.assertEqual(sum(m.get('text') == text for m in messages), 1)
        names = [await asyncio.to_thread(self.api.get, ident) for ident in (first, second)]
        self.assertEqual([w['name'] for w in names], ['billing', 'frontend'])


@unittest.skipIf(os.name == 'nt', 'Physical PTY smoke requires POSIX')
class _PtyCase(IsolatedCliServer):
    """Catches script-entry, VT input/resize, and real terminal ownership regressions."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='agentchattr-tui-pty-')
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.capture_path = self.directory / 'screen.json'
        self.terminal_env = dict(self.env, TERM='xterm-256color',
                                 AGENTCHATTR_PORT=str(self.ports[0]),
                                 AGENTCHATTR_MCP_HTTP_PORT=str(self.ports[1]),
                                 AGENTCHATTR_MCP_SSE_PORT=str(self.ports[2]),
                                 AGENTCHATTR_DATA_DIR=str(self.data_dir),
                                 AGENTCHATTR_UPLOAD_DIR=str(self.upload_dir))
        self.artifacts = Path(os.environ.get('TUI_QA_ARTIFACT_DIR', self.directory))
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def cli_command(self, *args):
        return [sys.executable, '-c', PTY_OBSERVER, str(self.capture_path), str(ROOT),
                '--url', self.url, '--no-resume', *args]

    def screen(self, terminal, predicate=lambda value: True):
        return terminal.screen(self.capture_path, predicate)

    def press(self, terminal, key, predicate=lambda value: True):
        before = self.screen(terminal)['count']
        terminal.key(key)
        return self.screen(terminal, lambda value: value['count'] > before and predicate(value))

    def paste(self, terminal, text):
        terminal.paste(text)
        return self.screen(terminal, lambda value: value['buffer'] == text)

    def button(self, terminal, caption):
        for _ in range(30):
            if self.screen(terminal)['focus_caption'] == caption:
                return self.press(terminal, 'Enter')
            self.press(terminal, 'Tab')
        self.fail('Button not keyboard reachable: ' + caption)

    def palette(self, terminal, action):
        self.press(terminal, 'F4', lambda s: 'Commands' in s['text'])
        self.paste(terminal, action)
        return self.press(terminal, 'Enter')

    def save(self, terminal, label):
        snapshot = self.screen(terminal)
        # Only isolated fixture content is captured. Redact auth tokens even on failure.
        def redact(text):
            text = re.sub(r'([?&]token=)[^\s&\"\'<>]+', r'\1[REDACTED]', text,
                          flags=re.IGNORECASE)
            return text.replace(self.token, '[REDACTED]')
        (self.artifacts / (label + '.vt.txt')).write_text(redact(terminal.output()))
        if terminal.process.poll() is not None:
            # Last renderer frame belongs to before exit; never label it as
            # post-restoration screen evidence.
            (self.artifacts / (label + '.exit.json')).write_text(json.dumps({
                'returncode': terminal.process.returncode, 'source': 'pty-process',
                'restoration_assertions_passed': True}))
            return snapshot
        (self.artifacts / (label + '.json')).write_text(redact(json.dumps(snapshot, ensure_ascii=False)))
        (self.artifacts / (label + '.txt')).write_text(redact(snapshot['text']))
        return snapshot

    def assert_restored(self, terminal):
        import termios
        self.assertEqual(terminal.process.wait(timeout=15), 0)
        terminal.wait(lambda: 'QA_TERMINAL_RETURNED' in terminal.output())
        self.assertEqual(termios.tcgetattr(terminal.slave), terminal.before_termios)
        self.assertIn('\x1b[?1049h', terminal.output())
        self.assertIn('\x1b[?1049l', terminal.output())
        self.assertIn('\x1b[?25h', terminal.output())
        self.assertNotIn('Full-screen unavailable', terminal.output())
        self.assertNotIn('unexpected local error', terminal.output())

    def assert_terminal_closed(self, terminal):
        self.assertIsNotNone(terminal.process.poll())
        self.assertFalse(terminal.reader.is_alive())
        for fd in (terminal.master, terminal.slave):
            with self.assertRaises(OSError):
                os.fstat(fd)
        (self.artifacts / (self._testMethodName + '.cleanup.json')).write_text(json.dumps({
            'child_reaped': True, 'reader_joined': True, 'pty_fds_closed': True}))


class TuiPtyIntegrationTests(_PtyCase):
    def test_script_resize_paste_navigation_cancel_mouse_and_quit(self):
        with PtyTerminal(self.cli_command(), env=self.terminal_env, cwd=ROOT) as terminal:
            self.addCleanup(self.assert_terminal_closed, terminal)
            self.screen(terminal, lambda s: 'Show archived' in s['text'])
            self.button(terminal, 'New session')
            self.screen(terminal, lambda s: 'Session name:' in s['text'])
            self.paste(terminal, 'pty-controls')
            self.press(terminal, 'Enter', lambda s: 'Connected' in s['text'] and 'Session name:' not in s['text'])
            draft = 'first line\nsecond line'
            self.paste(terminal, draft)
            self.press(terminal, 'Left')
            initial = self.save(terminal, 'pty-wide-120x30')
            self.assertEqual(initial['buffer_cursor'], len(draft) - 1)
            session = next(w for w in self.api.list()['workspaces'] if w['name'] == 'pty-controls')
            self.assertEqual(self.json_command('read', '--session', session['id']), [])
            for columns, rows, label in ((80, 24, 'pty-compact-80x24'), (70, 16, 'pty-small-70x16')):
                before = self.screen(terminal)['count']
                terminal.resize(columns, rows)
                self.screen(terminal, lambda s: (s['columns'], s['rows']) == (columns, rows)
                            and s['count'] > before
                            and ('Resize terminal' in s['text'] if columns == 70
                                 else 'Message' in s['text'] and 'New session' not in s['text']))
                self.save(terminal, label)
            self.press(terminal, 'F1', lambda s: 'F1/Esc Back' in s['text'] and '/channels' in s['text'])
            self.save(terminal, 'pty-small-help')
            self.press(terminal, 'Escape')
            before = self.screen(terminal)['count']
            terminal.resize(120, 30)
            restored = self.screen(terminal, lambda s: (s['columns'], s['rows']) == (120, 30)
                                   and s['count'] > before and s['buffer'] == draft
                                   and 'New session' in s['text'] and 'Clear draft' in s['text'])
            self.assertEqual(restored['buffer_cursor'], initial['buffer_cursor'])
            self.save(terminal, 'pty-wide-restored')
            self.press(terminal, 'F2', lambda s: 'Show archived' in s['text'])
            self.paste(terminal, 'pty-controls')
            self.save(terminal, 'pty-navigation-search')
            self.press(terminal, 'Escape', lambda s: s['buffer'] == draft)
            self.press(terminal, 'F5', lambda s: 'Activity' in s['text'])
            self.save(terminal, 'pty-activity')
            self.press(terminal, 'Escape', lambda s: s['buffer'] == draft)
            self.palette(terminal, 'Add agent')
            self.screen(terminal, lambda s: 'Working directory:' in s['text'])
            self.press(terminal, 'Tab')  # Provider radio -> working-directory field.
            terminal.send('\x01\x0b')
            self.screen(terminal, lambda s: s['buffer'] == '')
            self.paste(terminal, 'relative-invalid-cwd')
            self.press(terminal, 'Enter', lambda s: 'absolute existing directory' in s['text'])
            self.save(terminal, 'pty-validation-error')
            self.assertIn('relative-invalid-cwd', self.screen(terminal)['text'])
            self.press(terminal, 'Escape', lambda s: s['buffer'] == draft)
            # Actual SGR press/release on visible sidebar button.
            current = self.screen(terminal)
            y, row = next((y, row) for y, row in enumerate(current['text'].splitlines())
                          if 'New session' in row)
            x = row.index('New session') + 2
            terminal.send(f'\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m')
            self.screen(terminal, lambda s: 'Session name:' in s['text'])
            self.save(terminal, 'pty-mouse-dialog')
            self.press(terminal, 'Escape', lambda s: s['buffer'] == draft)
            self.press(terminal, 'CtrlC', lambda s: s['buffer'] == draft)
            self.press(terminal, 'CtrlQ', lambda s: 'Quit with unsent' in s['text'])
            self.save(terminal, 'pty-quit-confirmation')
            terminal.send('n')
            self.screen(terminal, lambda s: s['buffer'] == draft)
            self.press(terminal, 'CtrlQ', lambda s: 'Quit with unsent' in s['text'])
            terminal.send('y')
            self.assert_restored(terminal)
            self.save(terminal, 'pty-exited')


@unittest.skipUnless(os.name != 'nt' and shutil.which('tmux'), 'Requires POSIX tmux')
class TuiTmuxIntegrationTests(_PtyCase):
    @classmethod
    def environment_additions(cls):
        directory = Path(cls.temp.name) / 'bin'
        directory.mkdir()
        shim = directory / 'kilo'
        shim.write_text(f'#!{sys.executable}\nimport sys\n'
                        'print("INERT TUI AGENT READY", flush=True)\n'
                        'for line in sys.stdin:\n    print("INERT INPUT", flush=True)\n')
        shim.chmod(0o755)
        return {'PATH': str(directory) + os.pathsep + os.environ['PATH']}

    def setUp(self):
        super().setUp()
        # Own server has no user tmux config. Every cleanup precedes launch.
        self.addCleanup(self.cleanup_tmux)
        self.tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'qa-keeper',
                  'exec /bin/sleep 120')
        self.session = self.api.create(self._testMethodName)
        self.addCleanup(self.api.action, self.session['id'], 'archive')
        self.agent = self.api.action(self.session['id'], 'spawn', body={
            'provider': 'kilo', 'cwd': self.temp.name,
            'name': 'qa-inert-' + ('nested' if 'nested' in self._testMethodName else 'outside'),
            'history_mode': 'none'})
        def running():
            row = self.api.get(self.session['id'])['agents'][0]
            return row if row['last_state'] == 'running' else None
        self.agent = self.poll(running)
        self.target = self.agent['tmux_session']
        self.tmux('has-session', '-t', '=' + self.target)
        self.agent_pane = self.tmux('list-panes', '-t', self.target + ':',
                                    '-F', '#{pane_id}').stdout.strip()
        self.poll(lambda: 'INERT TUI AGENT READY' in self.tmux(
            'capture-pane', '-p', '-t', self.agent_pane).stdout)

    def tmux(self, *args, check=True):
        result = subprocess.run(['tmux', *args], env=self.terminal_env,
                                capture_output=True, text=True, timeout=5)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def cleanup_tmux(self):
        self.tmux('kill-server', check=False)
        self.poll(lambda: self.tmux('list-sessions', check=False).returncode != 0)
        socket = Path(self.terminal_env['TMUX_TMPDIR']) / ('tmux-' + str(os.getuid())) / 'default'
        # tmux may retain its socket inode after the server has exited. This is
        # our fixture's private path, also owned by TemporaryDirectory cleanup.
        socket.unlink(missing_ok=True)
        self.assertFalse(socket.exists())
        (self.artifacts / (self._testMethodName + '.tmux-cleanup.json')).write_text(json.dumps({
            'isolated_server_stopped': True, 'isolated_socket_removed': True}))

    def clients(self):
        result = self.tmux('list-clients', '-F', '#{client_name}|#{session_name}', check=False)
        return dict(line.split('|', 1) for line in result.stdout.splitlines())

    def open_attach(self, terminal):
        self.press(terminal, 'F3', lambda s: 'Choose agent' in s['text'])
        self.press(terminal, 'Enter', lambda s: 'Agent actions' in s['text'])
        self.paste(terminal, 'Attach')
        # Outside tmux this suspends rendering until detach; await real client
        # ownership below rather than demanding a new TUI frame here.
        terminal.key('Enter')

    def prepare_draft(self, terminal):
        self.screen(terminal, lambda s: 'Connected' in s['text'] and 'qa-inert' in s['text'])
        self.paste(terminal, 'survives attachment')
        self.press(terminal, 'Left')
        return self.screen(terminal)['buffer_cursor']

    def finish_with_draft(self, terminal):
        self.press(terminal, 'CtrlQ', lambda s: 'Quit with unsent' in s['text'])
        terminal.send('y')

    def test_outside_attach_detach_preserves_draft_and_repaints_delivery(self):
        with PtyTerminal(self.cli_command('--session', self.session['id']),
                         env=self.terminal_env, cwd=ROOT) as terminal:
            self.addCleanup(self.assert_terminal_closed, terminal)
            cursor = self.prepare_draft(terminal)
            self.save(terminal, 'tmux-outside-before')
            self.open_attach(terminal)
            terminal.wait(lambda: self.target in self.clients().values())
            frozen = self.screen(terminal)['count']
            self.json_command('send', '--session', self.session['id'], 'received during outside attach')
            # Authenticated persisted send completes while foreground owns tty.
            self.assertEqual(self.screen(terminal)['count'], frozen)
            terminal.send('\x02d')
            restored = self.screen(terminal, lambda s: s['count'] > frozen
                                   and s['buffer'] == 'survives attachment'
                                   and 'received during outside attach' in s['text'])
            self.assertEqual(restored['buffer_cursor'], cursor)
            self.save(terminal, 'tmux-outside-returned')
            self.assertFalse(self.clients())
            self.finish_with_draft(terminal)
            self.assert_restored(terminal)
            self.save(terminal, 'tmux-outside-exited')

    def test_nested_switch_return_and_tmux_selection_copy(self):
        import shlex
        tui_session = 'qa-tui-nested'
        self.addCleanup(self.tmux, 'kill-session', '-t', '=' + tui_session, check=False)
        command = shlex.join(self.cli_command('--session', self.session['id']))
        self.tmux('new-session', '-d', '-s', tui_session, '-x', '120', '-y', '30', command)
        self.tmux('set-option', '-t', tui_session, 'status', 'off')
        attach_command = [sys.executable, '-c',
                          'import fcntl,os,sys,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); '
                          'os.execvp(sys.argv[1],sys.argv[1:])',
                          'tmux', 'attach', '-t', '=' + tui_session]
        with PtyTerminal(attach_command, env=self.terminal_env, cwd=ROOT) as terminal:
            self.addCleanup(self.assert_terminal_closed, terminal)
            client = terminal.wait(lambda: next((c for c, s in self.clients().items()
                                                if s == tui_session), None))
            cursor = self.prepare_draft(terminal)
            self.save(terminal, 'tmux-nested-before')
            self.open_attach(terminal)
            terminal.wait(lambda: self.clients().get(client) == self.target)
            self.json_command('send', '--session', self.session['id'], 'copied terminal message')
            self.tmux('switch-client', '-c', client, '-l')
            terminal.wait(lambda: self.clients().get(client) == tui_session)
            self.screen(terminal, lambda s: s['buffer'] == 'survives attachment'
                        and 'copied terminal message' in s['text'])
            self.assertEqual(self.screen(terminal)['buffer_cursor'], cursor)
            self.save(terminal, 'tmux-nested-returned')
            self.press(terminal, 'F5', lambda s: 'Switch back: tmux switch-client -l' in s['text'])
            self.save(terminal, 'tmux-nested-guidance')
            self.press(terminal, 'Escape', lambda s: s['buffer'] == 'survives attachment')
            pane = self.tmux('list-panes', '-t', tui_session + ':', '-F', '#{pane_id}').stdout.strip()
            captured = self.tmux('capture-pane', '-p', '-t', pane).stdout
            self.assertIn('copied terminal message', captured)
            (self.artifacts / 'tmux-nested-capture-pane.txt').write_text(captured)
            self.tmux('copy-mode', '-t', pane)
            self.tmux('send-keys', '-t', pane, '-X', 'history-top')
            self.tmux('send-keys', '-t', pane, '-X', 'start-of-line')
            self.tmux('send-keys', '-t', pane, '-X', 'begin-selection')
            self.tmux('send-keys', '-t', pane, '-X', 'history-bottom')
            self.tmux('send-keys', '-t', pane, '-X', 'end-of-line')
            self.tmux('send-keys', '-t', pane, '-X', 'copy-selection-and-cancel')
            copied = self.tmux('show-buffer').stdout
            self.assertIn('copied terminal message', copied)
            (self.artifacts / 'tmux-native-selection-copy.txt').write_text(copied)
            self.finish_with_draft(terminal)
            terminal.wait(lambda: tui_session not in self.clients().values())
            self.assertEqual(terminal.process.wait(timeout=15), 0)
            self.assertIn('\x1b[?1049l', terminal.output())
