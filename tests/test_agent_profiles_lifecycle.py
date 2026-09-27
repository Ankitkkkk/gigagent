"""Saved profile and manager lifecycle contracts; inert process fixtures only."""
import unittest
from unittest.mock import patch
import test_workspace_launcher as fixtures


class ProfileLifecycleTests(unittest.TestCase):
    setUp = fixtures.LauncherTests.setUp
    queue = fixtures.LauncherTests.queue

    def spawn(self, **kw):
        return self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'none', **kw)

    def test_default_profile_is_saved(self):
        agent = self.spawn()
        self.assertEqual(agent.get('kind'), 'worker')
        self.assertEqual(agent.get('profile', {}).get('role'), 'generalist')

    def test_snapshot_locked_in_both_update_paths_and_shadow(self):
        agent = self.spawn(role='tester', personality='meticulous')
        aid = agent['agent_id']
        for field, value in [('kind', 'orchestrator'), ('profile', {}), ('profile', None)]:
            with self.assertRaisesRegex(ValueError, 'immutable'):
                self.store.update_agent(self.ws['id'], aid, **{field: value})
            with self.assertRaisesRegex(ValueError, 'immutable'):
                self.store.update_agent_if_launch(self.ws['id'], aid, agent['last_launch']['nonce'], **{field: value})
        self.assertEqual(self.store.read_identity(aid)['profile'], agent['profile'])
        agent['profile']['role'] = 'changed'
        self.assertEqual(self.store.get_agent(self.ws['id'], aid)['profile']['role'], 'tester')

    def test_startup_gate_prompt_and_callback_order(self):
        agent = self.spawn(role='tester', personality='concise')
        name = agent['registry_name']
        self.assertFalse(self.launcher.startup_ready(name))
        self.assertTrue(self.launcher.startup_ready('external'))
        calls = []
        self.launcher.on_startup_ready = lambda wid, aid: calls.append((self.launcher.startup_ready(name), self.queue(name)))
        self.launcher.on_heartbeat(name, True, 4242)
        self.assertTrue(self.launcher.startup_ready(name))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0])
        self.assertIn('tester', calls[0][1][0]['prompt'])
        self.assertIn('concise', calls[0][1][0]['prompt'])

    def test_orchestrator_singleton_stop_resume_archive(self):
        ws = self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        aid = ws['orchestrator']['agent_id']
        self.assertTrue(ws['orchestrator']['enabled'])
        again = self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        self.assertEqual(len(again['agents']), 1)
        self.launcher.stop(ws['id'], aid)
        self.assertFalse(self.store.get(ws['id'])['orchestrator']['enabled'])
        self.launcher.tick()
        self.assertEqual(len(self.popen_calls), 1)
        self.launcher.resume(ws['id'], aid, fresh=True)
        self.assertTrue(self.store.get(ws['id'])['orchestrator']['enabled'])
        self.store.set_archived(ws['id'], True)
        self.assertFalse(self.store.get(ws['id'])['orchestrator']['enabled'])

    def test_spawn_profile_validation_has_no_side_effects(self):
        for kw in ({'role': 'invented'}, {'personality': 'invented'}):
            with self.assertRaises(Exception):
                self.spawn(**kw)
        self.assertEqual(self.store.get(self.ws['id'])['agents'], [])
        self.assertEqual(self.popen_calls, [])

    def test_recovery_requires_verified_absence_and_is_bounded(self):
        ws = self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        aid = ws['orchestrator']['agent_id']
        self.store.update_agent(ws['id'], aid, last_state='exited')
        self.launcher._tmux.session_absent = lambda name: True
        self.launcher.tick()  # owned wrapper still alive
        self.assertEqual(len(self.popen_calls), 1)
        self.launcher._processes[aid].returncode = 0
        with patch.object(self.launcher, '_resume', side_effect=RuntimeError('inert failure')):
            for _ in range(6):
                self.clock.sleep(1000)
                self.launcher.tick()
        self.assertEqual(self.store.get(ws['id'])['orchestrator']['retry_count'], 3)
        self.assertIn('inert failure', self.store.get(ws['id'])['orchestrator']['last_error'])

    def test_recovery_uncertain_tmux_never_launches(self):
        ws = self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        aid = ws['orchestrator']['agent_id']
        self.store.update_agent(ws['id'], aid, last_state='exited')
        self.launcher._processes[aid].returncode = 0
        self.launcher._tmux.session_absent = lambda name: False
        self.launcher.tick()
        self.assertEqual(len(self.popen_calls), 1)
        self.assertEqual(self.store.get(ws['id'])['orchestrator']['retry_count'], 0)

    def test_concurrent_configuration_claims_one_saved_agent(self):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(lambda _: self.launcher.configure_orchestrator(
                self.ws['id'], 'claude', str(self.proj)), range(3)))
        self.assertEqual(len({ws['orchestrator']['agent_id'] for ws in rows}), 1)
        self.assertEqual(len(self.popen_calls), 1)

    def test_profile_snapshot_is_deep_copied_on_equal_update(self):
        agent = self.spawn()
        snapshot = agent['profile']
        self.store.update_agent(self.ws['id'], agent['agent_id'], profile=snapshot)
        snapshot['role_instructions'] = 'mutated'
        self.assertNotEqual(self.store.get_agent(self.ws['id'], agent['agent_id'])['profile'], snapshot)

    def test_legacy_profile_prompt_is_neutral(self):
        from agent_profiles import profile_prompt
        self.assertEqual(profile_prompt({'registry_name': 'legacy'}), '')

    def test_manager_only_receives_exact_explicit_mentions(self):
        ws = self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        manager = ws['agents'][0]
        self.launcher.on_heartbeat(manager['registry_name'], True, 4242)
        for tokens in ([], ['all'], ['both'], ['claude']):
            self.assertEqual(self.store.resolve_recipients(ws['channel'], tokens,
                                                          [manager['registry_name']]), [])
        self.assertEqual(self.store.resolve_recipients(ws['channel'], [manager['registry_name']], []),
                         [manager['agent_id']])

    def test_profile_prompt_precedes_new_worker_unread_bundle(self):
        agent = self.spawn()
        msg = self.messages.add('human', 'queued early', channel=self.ws['channel'])
        self.store.record_routing(self.ws['channel'], msg['id'], [agent['agent_id']])
        self.launcher.on_heartbeat(agent['registry_name'], True, 4242)
        queue = self.queue(agent['registry_name'])
        self.assertIn('Saved role:', queue[0]['prompt'])
        self.assertIn(f"message #{msg['id']}", queue[1]['prompt'])

    def test_archived_startup_cannot_send_prompt(self):
        agent = self.spawn()
        self.store.set_archived(self.ws['id'], True)
        self.launcher.on_heartbeat(agent['registry_name'], True, 4242)
        self.assertEqual(self.queue(agent['registry_name']), [])

    def test_profile_resume_and_fresh_preserve_snapshot(self):
        agent = self.spawn(role='debugger', personality='supportive')
        for fresh in (False, True):
            self.launcher.stop(self.ws['id'], agent['agent_id'])
            with patch.object(fixtures.ClaudeAdapter, 'locate_transcript', return_value=self.proj):
                resumed = self.launcher.resume(self.ws['id'], agent['agent_id'], fresh=fresh)
            self.assertEqual(resumed['profile'], agent['profile'])
            self.assertEqual(self.store.read_identity(agent['agent_id'])['profile'], agent['profile'])

    def test_recovery_relaunches_once_then_waits_for_readiness(self):
        ws = self.launcher.configure_orchestrator(self.ws['id'], 'kilo', str(self.proj))
        aid = ws['orchestrator']['agent_id']
        previous = ws['agents'][0]
        self.launcher._processes[aid].returncode = 0
        self.launcher._tmux.session_absent = lambda name: True
        self.registry.deregister(previous['registry_name'])
        self.store.mark_exited(previous['registry_name'])
        self.launcher.tick()
        self.launcher.tick()
        current = self.store.get_agent(ws['id'], aid)
        self.assertEqual(len(self.popen_calls), 2)
        self.assertEqual(current['last_state'], 'starting')
        self.assertNotEqual(current['last_launch']['nonce'], previous['last_launch']['nonce'])
        self.assertEqual(current['profile'], previous['profile'])
        self.assertFalse(self.launcher.startup_ready(current['registry_name']))

    def test_failed_stop_pauses_supervision_before_cleanup(self):
        ws = self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        aid = ws['orchestrator']['agent_id']
        with patch('workspace_launcher.stop_wrapper_process', side_effect=RuntimeError('uncertain')):
            with self.assertRaises(fixtures.LaunchError):
                self.launcher.stop(ws['id'], aid)
        self.assertFalse(self.store.get(ws['id'])['orchestrator']['enabled'])
        self.launcher.tick()
        self.assertEqual(len(self.popen_calls), 1)

    def test_remove_manager_releases_singleton_and_shadow(self):
        ws = self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        aid = ws['orchestrator']['agent_id']
        self.launcher.remove(ws['id'], aid)
        self.assertNotIn('orchestrator', self.store.get(ws['id']))
        self.assertIsNone(self.store.read_identity(aid))
        replacement = self.launcher.configure_orchestrator(ws['id'], 'claude', str(self.proj))
        self.assertNotEqual(replacement['orchestrator']['agent_id'], aid)

    def test_malformed_profile_inputs_raise_validation_errors(self):
        from agent_profiles import make_profile, validate_profile
        for value in ([], {}, None, 2):
            with self.assertRaises(ValueError):
                make_profile(role=value)
        for value in ({}, {'role': 'generalist'}, [], 'generalist'):
            with self.assertRaises(ValueError):
                validate_profile(value)

    def test_manager_instructions_are_frozen_in_snapshot(self):
        from agent_profiles import profile_prompt
        ws = self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        manager = ws['agents'][0]
        self.assertIn("chat_orchestrate(action='pending')", manager['profile']['role_instructions'])
        manager['profile']['role_instructions'] = 'saved routing contract'
        self.assertIn('saved routing contract', profile_prompt(manager))

    def test_store_manager_default_snapshot_contains_routing_contract(self):
        agent = self.store.add_agent(self.ws['id'], provider='claude', cwd=str(self.proj),
                                    history_mode='none', registry_name='manager', floor_id=0,
                                    native_session_id=None, history_state='done',
                                    last_launch={'nonce': 'test'}, kind='orchestrator')
        self.assertIn('chat_orchestrate', agent['profile']['role_instructions'])

    def test_low_level_legacy_worker_creation_has_no_invented_instructions(self):
        agent = self.store.add_agent(self.ws['id'], provider='claude', cwd=str(self.proj),
                                    history_mode='none', registry_name='legacy', floor_id=0,
                                    native_session_id=None, history_state='done',
                                    last_launch={'nonce': 'test'})
        self.assertIsNone(agent.get('profile'))

    def test_failed_manager_launch_can_be_configured_again(self):
        with patch.object(self.launcher, '_popen', side_effect=OSError('inert failure')):
            with self.assertRaises(fixtures.LaunchError):
                self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        previous = self.store.get(self.ws['id'])
        self.assertFalse(previous['orchestrator']['enabled'])
        resumed = self.launcher.configure_orchestrator(self.ws['id'], 'claude', str(self.proj))
        self.assertEqual(resumed['orchestrator']['agent_id'], previous['orchestrator']['agent_id'])
        self.assertTrue(resumed['orchestrator']['enabled'])
        self.assertEqual(resumed['agents'][0]['last_launch']['kind'], 'fresh')
