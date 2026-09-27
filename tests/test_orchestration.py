"""Original-message dispatch is authorized, durable, and bounded to one session."""
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests import test_workspace_launcher as fixtures
from orchestration import OrchestratorService, OrchestrationError, has_explicit_handle


class OrchestrationTests(unittest.TestCase):
    setUp = fixtures.LauncherTests.setUp

    def setup_service(self):
        self.manager = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none', name='routing-manager')
        self.worker = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'none', name='review-worker')
        # Service fixtures intentionally bypass public immutability: lifecycle is
        # covered separately and no provider process runs here.
        live = self.store._find(self.ws['id'])
        live['orchestrator'] = {'agent_id': self.manager['agent_id'], 'enabled': True}
        for row in live['agents']:
            row['kind'] = 'orchestrator' if row['agent_id'] == self.manager['agent_id'] else 'worker'
            row['last_state'] = 'running'
            row['last_launch']['startup_delivery_done'] = True
        self.store._commit()
        self.launcher.startup_ready = lambda name: bool((self.store.find_agent_by_registry_name(name)[1].get('last_launch') or {}).get('startup_delivery_done'))
        self.token = self.store.read_identity(self.manager['agent_id'])['token']
        self.service = OrchestratorService(path=self.data / 'orchestration.json', workspaces=self.store,
            messages=self.messages, registry=self.registry, agents=self.agents, launcher=self.launcher)
        self.message = self.messages.add('human', 'Please review this change', channel=self.ws['channel'])
        self.service.submit(self.ws['id'], self.message)
        return self.service

    def route(self, **kwargs):
        return self.service.call(self.token, 'route', message_id=self.message['id'],
                                 agent_ids=kwargs.pop('agent_ids', [self.worker['agent_id']]), **kwargs)

    def test_pending_contains_current_profile_roster_and_original(self):
        self.setup_service()
        response = self.service.call(self.token, 'pending')
        self.assertEqual(response['requests'][0]['message']['uid'], self.message['uid'])
        self.assertEqual([row['agent_id'] for row in response['workers']], [self.worker['agent_id']])
        self.assertIn('profile', response['workers'][0])

    def test_decision_records_original_routing_once_and_no_addressed_relay(self):
        self.setup_service()
        first = self.route(reason='Best reviewer')
        again = self.route(reason='Same task')
        self.assertEqual(first['status'], 'queued')
        self.assertEqual(again['status'], 'queued')
        queue = self.data / f"{self.worker['registry_name']}_queue.jsonl"
        entries = [json.loads(line) for line in queue.read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertIn(str(self.message['id']), entries[0]['prompt'])
        self.assertEqual(self.store.routing_for(self.ws['id'])[self.message['id']], [self.worker['agent_id']])
        notices = self.messages.get_since(self.message['id'])
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['sender'], 'system')
        self.assertEqual(notices[0]['type'], 'system')
        with self.assertRaises(OrchestrationError):
            self.route(agent_ids=[self.manager['agent_id']])

    def test_caller_must_own_current_enabled_manager_identity(self):
        self.setup_service()
        worker_token = self.store.read_identity(self.worker['agent_id'])['token']
        for token in ('', 'fake', worker_token):
            with self.assertRaises(OrchestrationError):
                self.service.call(token, 'pending')
        self.store._find(self.ws['id'])['orchestrator']['enabled'] = False
        with self.assertRaises(OrchestrationError):
            self.service.call(self.token, 'pending')

    def test_other_session_self_stopped_and_unready_workers_rejected(self):
        self.setup_service()
        other = self.store.create('other')
        foreign = self.launcher.spawn(other['id'], 'kilo', str(self.proj), 'none', name='foreign')
        for ids in ([foreign['agent_id']], [self.manager['agent_id']], [], ['missing']):
            with self.assertRaises(OrchestrationError):
                self.route(agent_ids=ids)
        self.store.update_agent(self.ws['id'], self.worker['agent_id'], last_state='exited')
        with self.assertRaises(OrchestrationError):
            self.route()
        self.store.update_agent(self.ws['id'], self.worker['agent_id'], last_state='running', last_launch={'nonce':'new'})
        with self.assertRaises(OrchestrationError):
            self.route()
        self.assertEqual(self.service.call(self.token, 'pending')['requests'][0]['status'], 'pending')

    def test_deleted_or_changed_message_cannot_dispatch(self):
        self.setup_service()
        self.messages.get_by_id(self.message['id'])['text'] = 'Changed request'
        with self.assertRaises(OrchestrationError):
            self.route()
        self.assertFalse((self.data / f"{self.worker['registry_name']}_queue.jsonl").exists())

    def test_dispatch_reservation_survives_restart_without_reenqueue(self):
        self.setup_service()
        with patch.object(self.agents, 'trigger_sync', side_effect=RuntimeError('queue unavailable')):
            result = self.route()
        self.assertEqual(result['status'], 'uncertain')
        self.service = OrchestratorService(path=self.data / 'orchestration.json', workspaces=self.store,
            messages=self.messages, registry=self.registry, agents=self.agents, launcher=self.launcher)
        with patch.object(self.agents, 'trigger_sync') as trigger:
            self.assertEqual(self.route()['status'], 'uncertain')
            self.service.tick()
        trigger.assert_not_called()
        self.assertIn(self.message['id'], self.store.routed_ids_for(self.ws['id'], self.worker['agent_id']))

    def test_pending_waits_for_ready_then_notifies_once_per_launch(self):
        self.setup_service()
        path = self.data / f"{self.manager['registry_name']}_queue.jsonl"
        path.unlink(missing_ok=True)
        self.service._notified.clear()
        self.store.update_agent(self.ws['id'], self.manager['agent_id'], last_launch={'nonce': 'next'})
        self.service.tick()
        self.assertFalse(path.exists())
        self.store.update_agent(self.ws['id'], self.manager['agent_id'],
                                last_launch={'nonce':'next', 'startup_delivery_done':True})
        self.service.tick()
        self.service.tick()
        self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_ledger_write_failure_never_enqueues(self):
        self.setup_service()
        with patch.object(self.service, '_save', side_effect=OSError('disk full')), \
                patch.object(self.agents, 'trigger_sync') as trigger:
            with self.assertRaises(OSError):
                self.route()
        trigger.assert_not_called()

    def test_explicit_handles_include_typos_but_not_emails(self):
        for text in ('@all hi', 'please @missing-agent look', '(@worker)'):
            self.assertTrue(has_explicit_handle(text))
        for text in ('review it', 'email me@example.com', 'my_name@host.test'):
            self.assertFalse(has_explicit_handle(text))

    def test_blocked_floor_and_private_original_are_not_dispatched(self):
        self.setup_service()
        for floor in (None, self.message['id'] + 1):
            self.store.update_agent(self.ws['id'], self.worker['agent_id'], floor_id=floor)
            with self.assertRaises(OrchestrationError):
                self.route()
            self.assertEqual(self.service._requests[self.service._key(self.ws['id'], self.message['id'])]['status'], 'pending')
        self.store.update_agent(self.ws['id'], self.worker['agent_id'], floor_id=0)
        private = self.messages.add('human', 'private request', channel=self.ws['channel'],
                                    metadata={'audience':[self.manager['agent_id'], 'different-worker']})
        self.service.submit(self.ws['id'], private)
        with self.assertRaises(OrchestrationError):
            self.service.call(self.token, 'route', message_id=private['id'], agent_ids=[self.worker['agent_id']])
        self.assertFalse((self.data / 'review-worker_queue.jsonl').exists())

    def test_hidden_manager_cannot_return_or_dispatch_known_original(self):
        self.setup_service()
        for status in ('pending', 'queued'):
            if status == 'queued':
                self.store.update_agent(self.ws['id'], self.manager['agent_id'], floor_id=0)
                self.route()
            self.store.update_agent(self.ws['id'], self.manager['agent_id'], floor_id=self.message['id'] + 1)
            self.assertEqual(self.service.call(self.token, 'pending')['requests'], [])
            with self.assertRaises(OrchestrationError):
                self.route()
