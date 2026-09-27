"""Observer routing and MCP authorization use the production integration seams."""
import asyncio
import json
import unittest
from unittest.mock import patch

import app as application
import mcp_bridge
from tests import test_orchestration as fixtures


class RoutingTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    setup_service = fixtures.OrchestrationTests.setup_service

    def bind(self, default='all'):
        self.setup_service()
        from router import Router
        self.router = Router(agent_names=[self.manager['registry_name'], self.worker['registry_name']],
                             default_mention=default, online_checker=lambda: set(self.registry.get_active_names()))
        for module, name, value in (
                (application, 'store', self.messages), (application, 'workspace_store', self.store),
                (application, 'registry', self.registry), (application, 'agents', self.agents),
                (application, 'router', self.router), (application, 'orchestration', self.service),
                (application, 'workspace_launcher', self.launcher), (application, 'session_engine', None),
                (application, 'config', self.config), (mcp_bridge, 'orchestration_service', self.service),
                (mcp_bridge, 'registry', self.registry)):
            p = patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)
        for path in self.data.glob('*_queue.jsonl'):
            path.unlink()

    def post(self, text, sender='human', kind='chat'):
        message = self.messages.add(sender, text, msg_type=kind, channel=self.ws['channel'])
        asyncio.run(application._handle_new_message(message))
        return message

    def test_unmentioned_human_default_all_only_wakes_orchestrator(self):
        self.bind()
        message = self.post('Plan this change')
        self.assertTrue((self.data / 'routing-manager_queue.jsonl').exists())
        self.assertFalse((self.data / 'review-worker_queue.jsonl').exists())
        self.assertIn(message['id'], [r['message']['id'] for r in self.service.call(self.token)['requests']])

    def test_explicit_mentions_bypass_manager_and_unknown_never_broadcasts(self):
        self.bind()
        self.post('@review-worker review')
        self.assertFalse((self.data / 'routing-manager_queue.jsonl').exists())
        worker_queue = self.data / 'review-worker_queue.jsonl'
        self.assertTrue(worker_queue.exists())
        before = worker_queue.read_text()
        self.post('@typo-worker look')
        self.assertEqual(worker_queue.read_text(), before)
        self.assertFalse((self.data / 'routing-manager_queue.jsonl').exists())

    def test_group_mentions_exclude_manager_and_manager_prose_never_relays(self):
        self.bind()
        self.post('@all status')
        self.assertFalse((self.data / 'routing-manager_queue.jsonl').exists())
        worker_queue = self.data / 'review-worker_queue.jsonl'
        before = worker_queue.read_text()
        self.post('@review-worker review it', sender='routing-manager')
        self.assertEqual(worker_queue.read_text(), before)

    def test_startup_gate_keeps_original_unread_without_early_enqueue(self):
        self.bind()
        self.store.update_agent(self.ws['id'], self.worker['agent_id'], last_launch={'nonce':'starting'})
        message = self.post('@review-worker early request')
        self.assertFalse((self.data / 'review-worker_queue.jsonl').exists())
        self.assertIn(message['id'], self.store.routed_ids_for(self.ws['id'], self.worker['agent_id']))

    def test_agent_reply_and_summary_do_not_create_requests(self):
        self.bind()
        before = len(self.service._requests)
        self.post('Done', sender='review-worker')
        self.post('Status summary', kind='summary')
        self.assertEqual(len(self.service._requests), before)

    def test_mcp_tool_requires_bearer_token_not_claimed_name(self):
        self.bind()
        with patch.object(mcp_bridge, '_extract_agent_token', return_value=''):
            self.assertIn('Error:', mcp_bridge.chat_orchestrate())
        with patch.object(mcp_bridge, '_extract_agent_token', return_value=self.token):
            result = json.loads(mcp_bridge.chat_orchestrate())
        self.assertEqual(result['workspace_id'], self.ws['id'])

    def test_trusted_human_kind_survives_agent_name_collision(self):
        self.bind()
        message = self.messages.add(self.worker['registry_name'], 'Review please',
                                    channel=self.ws['channel'], actor_kind='human')
        asyncio.run(application._handle_new_message(message))
        self.assertIn(message['id'], [r['message']['id'] for r in self.service.call(self.token)['requests']])
        self.service.call(self.token, 'route', message_id=message['id'], agent_ids=[self.worker['agent_id']])
        self.assertIn(message['id'], [m['id'] for m in self.launcher.unread_for(self.ws['id'], self.worker['agent_id'])])

    def test_trusted_agent_kind_survives_stale_name(self):
        self.bind()
        before = len(self.service._requests)
        message = self.messages.add('old-no-longer-registered-name', 'Done',
                                    channel=self.ws['channel'], actor_kind='agent')
        asyncio.run(application._handle_new_message(message))
        self.assertEqual(len(self.service._requests), before)
        self.assertFalse((self.data / 'review-worker_queue.jsonl').exists())

    def test_direct_request_at_startup_drain_boundary_is_delivered_once(self):
        import threading
        self.bind()
        launch = dict(self.worker['last_launch'], startup_delivery_done=False, identity_prompt_sent=False)
        self.store.update_agent(self.ws['id'], self.worker['agent_id'], last_launch=launch)
        reached = threading.Event()
        finished = threading.Event()
        errors = []
        original_delivery = application._deliver_workspace_message
        message = self.messages.add('human', '@review-worker interleaved', channel=self.ws['channel'])
        def delivery(*args):
            reached.set()
            return original_delivery(*args)
        def observer():
            try:
                asyncio.run(application._handle_new_message(message))
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
        thread = threading.Thread(target=observer, daemon=True)
        def boundary(*args):
            thread.start()
            self.assertTrue(reached.wait(5), 'observer did not reach delivery boundary')
        with patch.object(application, '_deliver_workspace_message', side_effect=delivery), \
                patch.object(self.launcher, '_send_bundle', side_effect=boundary):
            self.launcher._after_ready(self.ws['id'], self.worker['agent_id'], launch['nonce'])
            self.assertTrue(finished.wait(5), 'observer stayed blocked after startup')
        thread.join(5)
        self.assertFalse(errors)
        entries = [json.loads(line) for line in (self.data / 'review-worker_queue.jsonl').read_text().splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertIn('Saved role:', entries[0]['prompt'])
        self.assertEqual(entries[1]['text'], 'human: @review-worker interleaved')

    def test_late_original_survives_reservation_crash_and_mcp_ack_then_restart(self):
        from orchestration import OrchestratorService
        self.bind()
        later = self.messages.add('human', '@review-worker direct later', channel=self.ws['channel'])
        self.store.record_routing(self.ws['channel'], later['id'], [self.worker['agent_id']])
        self.store.ack(self.ws['id'], self.worker['agent_id'], [later['id']])
        with patch.object(self.store, 'record_routing', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.service.call(self.token, 'route', message_id=self.message['id'], agent_ids=[self.worker['agent_id']])
        def restart():
            return OrchestratorService(path=self.data / 'orchestration.json', workspaces=self.store,
                messages=self.messages, registry=self.registry, agents=self.agents, launcher=self.launcher)
        self.service = restart()
        self.assertEqual([m['id'] for m in self.launcher.unread_for(self.ws['id'], self.worker['agent_id'])],
                         [self.message['id']])
        with patch.object(mcp_bridge, 'store', self.messages), \
                patch.object(mcp_bridge, 'workspace_policy', self.store.policy_for), \
                patch.object(mcp_bridge, 'workspace_ack', self.store.ack_by_name), \
                patch.object(mcp_bridge, 'workspace_late_messages', application._late_messages_for_agent), \
                patch.object(mcp_bridge, '_cursors', {self.worker['registry_name']:{self.ws['channel']:later['id']}}), \
                patch.object(mcp_bridge, '_CURSORS_FILE', None), \
                patch.object(mcp_bridge, '_extract_agent_token', return_value=self.store.read_identity(self.worker['agent_id'])['token']):
            result = mcp_bridge.chat_read(sender=self.worker['registry_name'], channel=self.ws['channel'])
            self.assertIn('Please review this change', result)
            self.assertEqual(mcp_bridge._cursors[self.worker['registry_name']][self.ws['channel']], later['id'])
        self.assertFalse(self.launcher.unread_for(self.ws['id'], self.worker['agent_id']))
        restart().tick()
        restart().tick()
        self.assertFalse(self.launcher.unread_for(self.ws['id'], self.worker['agent_id']))
        self.assertFalse((self.data / 'review-worker_queue.jsonl').exists())

    def test_stale_name_reuse_cannot_redirect_private_session_delivery(self):
        self.bind()
        old_name = self.worker['registry_name']
        message = self.messages.add('human', '@review-worker private original', channel=self.ws['channel'])
        aid = self.worker['agent_id']
        self.registry.rename(old_name, 'renamed-worker')
        self.store.rename_agent(old_name, 'renamed-worker')
        other = self.store.create('foreign')
        foreign = self.launcher.spawn(other['id'], 'claude', str(self.proj), 'none', name=old_name)
        self.store.update_agent(other['id'], foreign['agent_id'], last_state='running',
            last_launch=dict(foreign['last_launch'], startup_delivery_done=True))
        application._deliver_workspace_message(message, [aid], [aid], '', None,
            lambda ids: self.store.record_routing(self.ws['channel'], message['id'], ids))
        self.assertFalse((self.data / f'{old_name}_queue.jsonl').exists())
        self.assertIn('private original', (self.data / 'renamed-worker_queue.jsonl').read_text())

    def test_token_guard_refuses_new_owner_between_resolution_and_queue(self):
        self.bind()
        name = self.worker['registry_name']
        token = self.store.read_identity(self.worker['agent_id'])['token']
        self.registry.deregister(name)
        self.registry.register('claude', preferred_name=name, allow_reserved=True)
        with self.assertRaises(ValueError):
            self.agents.trigger_sync(name, message='private original', expected_token=token)
        self.assertFalse((self.data / f'{name}_queue.jsonl').exists())
