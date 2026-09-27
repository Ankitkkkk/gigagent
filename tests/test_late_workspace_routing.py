"""Late original assignments must remain unread without cursor rewind."""
import unittest
import test_workspace_launcher as fixtures
from workspace_store import WorkspaceStore


class LateRoutingTests(unittest.TestCase):
    setUp = fixtures.LauncherTests.setUp
    queue = fixtures.LauncherTests.queue

    def prepare(self):
        agent = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'none')
        self.launcher.on_heartbeat(agent['registry_name'], True, 4242)
        for _ in range(100):
            self.messages.add('human', 'older', channel='elsewhere')
        original = self.messages.add('human', 'pending original', channel=self.ws['channel'])
        direct = self.messages.add('human', 'direct later', channel=self.ws['channel'])
        self.assertEqual([original['id'], direct['id']], [100, 101])
        self.store.record_routing(self.ws['channel'], 100, [])
        self.store.record_routing(self.ws['channel'], 101, [agent['agent_id']])
        self.store.ack(self.ws['id'], agent['agent_id'], [101])
        self.assertEqual(self.store.get_agent(self.ws['id'], agent['agent_id'])['read_mark'], 101)
        self.store.record_routing(self.ws['channel'], 100, [agent['agent_id']])
        return agent

    def test_late_original_remains_unread_and_retryable_after_newer_ack(self):
        agent = self.prepare()
        for _ in range(2):
            self.assertEqual([m['id'] for m in self.launcher.unread_for(self.ws['id'], agent['agent_id'])], [100])
        self.launcher.retry(self.ws['id'], agent['agent_id'])
        self.assertIn('message #100', self.queue(agent['registry_name'])[-1]['prompt'])
        self.assertEqual(self.store.get_agent(self.ws['id'], agent['agent_id'])['read_mark'], 101)

    def test_late_ack_survives_reload_and_duplicate_assignment(self):
        agent = self.prepare()
        self.store.ack(self.ws['id'], agent['agent_id'], [100])
        reloaded = WorkspaceStore(self.data / 'workspaces.json', self.data / 'identity')
        reloaded.record_routing(self.ws['channel'], 100, [agent['agent_id']])
        self.launcher.store = reloaded
        self.assertEqual(self.launcher.unread_for(self.ws['id'], agent['agent_id']), [])
        self.assertEqual(reloaded.get_agent(self.ws['id'], agent['agent_id'])['read_mark'], 101)

    def test_policy_exposes_late_pending_until_explicit_ack(self):
        agent = self.prepare()
        self.assertEqual(self.store.policy_for(agent['registry_name'], self.ws['channel']).get('late_unacked_ids'), [100])
        self.store.ack(self.ws['id'], agent['agent_id'], [101])
        self.assertEqual(self.store.policy_for(agent['registry_name'], self.ws['channel']).get('late_unacked_ids'), [100])
        self.store.ack(self.ws['id'], agent['agent_id'], [100])
        self.assertEqual(self.store.policy_for(agent['registry_name'], self.ws['channel']).get('late_unacked_ids'), [])

    def test_late_ack_works_when_live_routing_was_pruned(self):
        agent = self.prepare()
        # Simulate old-state pruning; durable per-agent exception remains authoritative.
        with self.store._lock:
            self.store._find(self.ws['id'])['routing'].pop('100', None)
        self.assertEqual([m['id'] for m in self.launcher.unread_for(self.ws['id'], agent['agent_id'])], [100])
        self.store.ack(self.ws['id'], agent['agent_id'], [100])
        self.assertEqual(self.launcher.unread_for(self.ws['id'], agent['agent_id']), [])

    def test_duplicate_pending_assignment_and_new_recipient_preserve_individual_acks(self):
        first = self.prepare()
        second = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'literal')
        self.launcher.on_heartbeat(second['registry_name'], True, 4242)
        self.store.record_routing(self.ws['channel'], 101, [second['agent_id']])
        self.store.ack(self.ws['id'], second['agent_id'], [101])
        self.store.record_routing(self.ws['channel'], 100, [first['agent_id']])
        self.assertEqual(self.store.get_agent(self.ws['id'], first['agent_id'])['late_unacked_ids'], [100])
        self.store.ack(self.ws['id'], first['agent_id'], [100])
        self.store.record_routing(self.ws['channel'], 100, [first['agent_id'], second['agent_id']])
        self.assertEqual(self.launcher.unread_for(self.ws['id'], first['agent_id']), [])
        self.assertEqual([m['id'] for m in self.launcher.unread_for(self.ws['id'], second['agent_id'])], [100])

    def test_late_pending_survives_restart_before_read(self):
        agent = self.prepare()
        self.launcher.store = WorkspaceStore(self.data / 'workspaces.json', self.data / 'identity')
        self.assertEqual([m['id'] for m in self.launcher.unread_for(self.ws['id'], agent['agent_id'])], [100])
        self.assertEqual(self.launcher.store.get_agent(self.ws['id'], agent['agent_id'])['read_mark'], 101)
