"""Human /continue must wake the recipients stopped by the loop guard."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))
import app
from fastapi import WebSocketDisconnect
from _workspace_helpers import app_cfg


class ContinueTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ('store', 'rules', 'summaries', 'jobs', 'schedules', 'router', 'agents',
                     'registry', 'session_store', 'session_engine', 'config', 'workspace_store',
                     'session_token', 'room_settings', 'agent_hats', '_event_loop', 'ws_clients',
                     '_loop_guard_pending'):
            stack.enter_context(patch.object(app, name, getattr(app, name)))
        app._event_loop = None
        app.ws_clients = set()
        app.configure(app_cfg(self.root), session_token='test')
        app.router.max_hops = 0
        self.ws = app.workspace_store.create('work')
        self.other = app.workspace_store.create('other')
        self.add('claude', 'writer', self.ws)
        self.reader = self.add('codex', 'reviewer', self.ws)
        self.add('codex', 'outsider', self.other)

    def add(self, provider, name, ws):
        agent = app.workspace_store.add_agent(ws['id'], provider=provider, registry_name=name,
            cwd=str(self.root), history_mode='none', floor_id=0, native_session_id=None,
            history_state='done', last_launch={'kind': 'spawn', 'nonce': name})
        app.workspace_store.update_agent(ws['id'], agent['agent_id'], last_state='running')
        app.registry.register(provider, preferred_name=name, allow_reserved=True)
        return agent

    def post(self, text, sender='writer', ws=None):
        msg = app.store.add(sender, text, channel=(ws or self.ws)['channel'])
        asyncio.run(app._handle_new_message(msg))

    def queue(self, name):
        path = self.root / f'{name}_queue.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def websocket_continue(self, sender='human', ws=None):
        frames = []
        channel = (ws or self.ws)['channel']
        class Socket:
            query_params = {'token': 'test'}
            sent = False
            async def accept(self): pass
            async def send_text(self, raw): frames.append(json.loads(raw))
            async def receive_text(self):
                if self.sent:
                    raise WebSocketDisconnect()
                self.sent = True
                return json.dumps({'type': 'message', 'sender': sender, 'text': '/continue',
                                   'channel': channel, 'request_id': 'resume'})
        asyncio.run(app.websocket_endpoint(Socket()))
        return next(frame['data'] for frame in frames if frame['type'] == 'message_sent')

    def test_websocket_continue_wakes_blocked_recipient_once(self):
        self.post('@reviewer please check')
        self.assertTrue(app.router.is_paused(self.ws['channel']))
        self.assertEqual(self.queue('reviewer'), [])
        result = self.websocket_continue()
        self.assertTrue(result['ok'])
        self.assertFalse(app.router.is_paused(self.ws['channel']))
        self.assertEqual(len(self.queue('reviewer')), 1)
        self.assertEqual(self.queue('reviewer')[0]['channel'], self.ws['channel'])
        self.websocket_continue()
        self.assertEqual(len(self.queue('reviewer')), 1)

    def test_observer_continue_wakes_only_current_session(self):
        self.post('@all please check')
        self.post('@outsider please check', sender='reviewer', ws=self.other)
        self.post('/continue', sender='human')
        self.assertEqual(len(self.queue('reviewer')), 1)
        self.assertEqual(self.queue('writer'), [])
        self.assertEqual(self.queue('outsider'), [])
        self.assertTrue(app.router.is_paused(self.other['channel']))

    def test_messages_during_pause_are_deduplicated_and_resumed(self):
        self.post('@reviewer first')
        self.post('@reviewer second')
        self.post('@writer reply', sender='reviewer')
        self.websocket_continue()
        self.assertEqual(len(self.queue('reviewer')), 1)
        self.assertEqual(len(self.queue('writer')), 1)

    def test_agent_cannot_continue_over_either_entrypoint(self):
        self.post('@reviewer check')
        self.post('/continue', sender='writer')
        self.assertTrue(app.router.is_paused(self.ws['channel']))
        self.assertFalse(self.websocket_continue(sender='writer')['ok'])
        self.assertTrue(app.router.is_paused(self.ws['channel']))
        self.assertEqual(self.queue('reviewer'), [])

    def test_stopped_recipient_is_not_woken_by_continue(self):
        self.post('@reviewer check')
        app.workspace_store.update_agent(self.ws['id'], self.reader['agent_id'], last_state='exited')
        self.websocket_continue()
        self.assertEqual(self.queue('reviewer'), [])

    def test_human_message_discards_old_deferred_wakes(self):
        self.post('@reviewer check')
        self.post('change of plan', sender='human')
        self.websocket_continue()
        self.assertEqual(self.queue('reviewer'), [])

    def test_renamed_recipient_is_resumed_by_stable_id(self):
        self.post('@reviewer check')
        app.registry.rename('reviewer', 'renamed-reviewer')
        app.workspace_store.rename_agent('reviewer', 'renamed-reviewer')
        self.websocket_continue()
        self.assertEqual(len(self.queue('renamed-reviewer')), 1)
        self.assertEqual(self.queue('reviewer'), [])

    def test_removed_recipient_does_not_wake_replacement_with_same_name(self):
        self.post('@reviewer check')
        app.workspace_store.remove_agent(self.ws['id'], self.reader['agent_id'])
        app.registry.deregister('reviewer')
        self.add('codex', 'reviewer', self.ws)
        self.websocket_continue()
        self.assertEqual(self.queue('reviewer'), [])

    def test_failed_wake_can_retry_without_repeating_successful_wake(self):
        self.post('@reviewer check')
        self.post('@writer check', sender='reviewer')
        trigger = app.agents.trigger
        async def fail_one(name, **kwargs):
            if name == 'reviewer':
                raise OSError('queue unavailable')
            return await trigger(name, **kwargs)
        with patch.object(app.agents, 'trigger', side_effect=fail_one):
            with self.assertLogs('app', level='ERROR'):
                self.assertFalse(self.websocket_continue()['ok'])
        self.assertEqual(len(self.queue('writer')), 1)
        self.assertTrue(self.websocket_continue()['ok'])
        self.assertEqual(len(self.queue('writer')), 1)
        self.assertEqual(len(self.queue('reviewer')), 1)

    def test_session_turn_guard_is_rechecked_on_continue(self):
        self.post('@reviewer check')
        with patch.object(app.session_engine, 'get_allowed_agent', return_value='writer'):
            self.websocket_continue()
        self.assertEqual(self.queue('reviewer'), [])
