import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from providers.base import ProviderAdapter
from providers.codex import CodexAdapter
from prompt_signals import PromptMonitor
from waiting_hooks import configure_stream, emit, install


MCP_PROMPT = b'''Calling yapp.chat_send
Field 1/1
Allow the yapp MCP server to run tool "chat_send"?
channel: example
message: hello
> 1. Allow                     Run the tool and continue.
  2. Allow for this session    Remember this choice.
  3. Always allow              Remember for future calls.
  4. Cancel                    Cancel this tool call
enter to submit | esc to cancel
'''


def payload(event='PermissionRequest', tool='mcp__yapp__chat_send', turn='t1'):
    return dict(hook_event_name=event, session_id='s1', turn_id=turn,
                tool_name=tool, tool_input={'secret': 'DO-NOT-RECORD'}, transcript_path='/private')


class PromptTests(unittest.TestCase):
    def test_real_relay_uses_launch_adapter_instead_of_builtin_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / 'events'
            events.mkdir()
            (root / 'custom_prompt_adapter.py').write_text('''
from providers.base import ProviderAdapter
from prompt_signals import PromptEvent
class Custom(ProviderAdapter):
    def prompt_event(self, payload):
        if payload.get('hook_event_name') == 'CustomPermission':
            return PromptEvent('requested', 's1', 't1', 'CustomTool')
''')
            configure_stream(events, 'codex', {'adapter': 'custom_prompt_adapter:Custom'})
            env = dict(os.environ, YAPP_PROMPT_EVENTS=str(events), PYTHONPATH=str(root))
            script = Path(__file__).resolve().parents[1] / 'waiting_hooks.py'
            result = subprocess.run([sys.executable, str(script), 'emit', '--provider', 'codex'],
                input=json.dumps({'hook_event_name': 'CustomPermission'}), text=True,
                capture_output=True, env=env, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
            monitor = PromptMonitor(ProviderAdapter(), events)
            self.assertTrue(monitor.observe(b'Unknown custom permission'))

    def test_event_published_during_poll_is_not_dropped_as_future(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            monitor = PromptMonitor(CodexAdapter(), path, clock=lambda: 100)
            emit('codex', payload(), path, now=100.001)
            self.assertTrue(monitor.observe(b'Unknown approval'))

    def test_concurrent_events_are_complete_and_backlog_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with ThreadPoolExecutor(max_workers=4) as pool:
                emitted = list(pool.map(lambda _: emit('codex', payload(), path), range(12)))
            self.assertTrue(all(emitted))
            self.assertEqual(len(list(path.glob('*.json'))), 12)
            monitor = PromptMonitor(CodexAdapter(), path)
            self.assertTrue(monitor.observe(b'Unknown prompt'))
            for _ in range(11):
                emit('codex', payload('PostToolUse'), path)
            self.assertTrue(monitor.observe(b'Unknown prompt'))
            emit('codex', payload('SessionEnd'), path)
            self.assertFalse(monitor.observe(b'Idle'))
            for i in range(256):
                (path / f'{i}.json').write_text('invalid')
            self.assertFalse(emit('codex', payload(), path))
            self.assertFalse(monitor.observe(b'Idle'))
            self.assertFalse(list(path.glob('*.json')))

    def test_codex_mcp_modal_and_dismissal(self):
        adapter = CodexAdapter()
        self.assertTrue(adapter.waiting_for_input(MCP_PROMPT))
        self.assertFalse(adapter.waiting_for_input(MCP_PROMPT + b'Completed.\n> Ask anything\n'))
        self.assertFalse(adapter.waiting_for_input(b'Feedback form\nenter to submit | esc to cancel'))
        self.assertTrue(ProviderAdapter().waiting_for_input(b'Continue? [y/N]'))

    def test_launch_isolation_clear_matching_tool_and_expiry(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            monitor = PromptMonitor(CodexAdapter(), Path(first), clock=lambda: 100)
            other = PromptMonitor(CodexAdapter(), Path(second), clock=lambda: 100)
            self.assertTrue(emit('codex', payload(), Path(first), now=100))
            self.assertTrue(monitor.observe(b'Unrecognized approval dialog'))
            self.assertFalse(other.observe(b'Idle'))
            emit('codex', payload('PostToolUse', tool='Bash'), Path(first), now=100)
            self.assertTrue(monitor.observe(b'Unrecognized approval dialog'))
            emit('codex', payload('PostToolUse'), Path(first), now=100)
            self.assertFalse(monitor.observe(b'Working'))
            emit('codex', payload(), Path(first), now=100)
            self.assertTrue(monitor.observe(b'Unrecognized approval dialog'))
            monitor.clock = lambda: 161
            self.assertFalse(monitor.observe(b'Idle'))
            emit('codex', payload(), Path(first), now=100)
            self.assertFalse(monitor.observe(b'Idle'), 'late stale event must not revive a hint')

    def test_visible_dismissal_clears_before_long_tool_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = PromptMonitor(CodexAdapter(), Path(directory))
            emit('codex', payload(), Path(directory))
            self.assertTrue(monitor.observe(MCP_PROMPT))
            self.assertFalse(monitor.observe(b'Working (esc to interrupt)'))
            self.assertFalse(monitor.observe(b'Working (esc to interrupt)'))

    def test_parallel_tools_and_turn_scoped_clearing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            monitor = PromptMonitor(CodexAdapter(), path)
            emit('codex', payload(turn='t1'), path)
            emit('codex', payload(turn='t2'), path)
            self.assertTrue(monitor.observe(b'Unknown modal'))
            emit('codex', payload('Stop', turn='t1'), path)
            self.assertTrue(monitor.observe(b'Unknown modal'))
            emit('codex', payload('Interrupt', turn='t2'), path)
            self.assertFalse(monitor.observe(b'Idle'))

    def test_hook_payload_validation_and_no_private_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.assertFalse(emit('codex', {'hook_event_name': 'PermissionRequest'}, path))
            self.assertFalse(emit('claude', payload(), path))
            self.assertTrue(emit('codex', payload(), path))
            content = next(path.glob('*.json')).read_text()
            self.assertNotIn('DO-NOT-RECORD', content)
            self.assertNotIn('/private', content)
            self.assertNotIn('tool_input', content)
            self.assertFalse(PromptMonitor(CodexAdapter(), path).observe(None))

    def test_hook_command_is_silent_and_inert_outside_wrapper(self):
        script = Path(__file__).resolve().parents[1] / 'waiting_hooks.py'
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, YAPP_PROMPT_EVENTS=directory)
            result = subprocess.run([sys.executable, str(script), 'emit', '--provider', 'codex'],
                input=json.dumps(payload()), text=True, capture_output=True, env=env, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
            self.assertTrue(list(Path(directory).glob('*.json')))
            env.pop('YAPP_PROMPT_EVENTS')
            result = subprocess.run([sys.executable, str(script), 'emit', '--provider', 'codex'],
                input='not-json', text=True, capture_output=True, env=env, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_install_preserves_existing_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            path = project / '.codex' / 'hooks.json'
            path.parent.mkdir()
            existing = {'description': 'mine', 'hooks': {'PermissionRequest': [
                {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo existing'}]}]}}
            path.write_text(json.dumps(existing))
            self.assertEqual(install('codex', project), path)
            content = path.read_bytes()
            install('codex', project)
            self.assertEqual(path.read_bytes(), content)
            data = json.loads(content)
            self.assertEqual(data['hooks']['PermissionRequest'][0], existing['hooks']['PermissionRequest'][0])
            self.assertEqual(data['hooks']['PermissionRequest'][1]['matcher'], '.*')
            self.assertIn('Interrupt', data['hooks'])
            self.assertNotIn('dangerously', content.decode())
            path.write_text('{broken')
            with self.assertRaises(ValueError): install('codex', project)
            self.assertEqual(path.read_text(), '{broken')

    def test_custom_adapter_controls_terminal_detection(self):
        from wrapper_unix import get_activity_checker
        class Custom(ProviderAdapter):
            def waiting_for_input(self, output):
                return output == b'Custom confirmation'
        checker = get_activity_checker('test', adapter=Custom())
        with patch('wrapper_unix.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 0, stdout=b'Custom confirmation')):
            checker()
            self.assertTrue(checker.waiting_for_input)
