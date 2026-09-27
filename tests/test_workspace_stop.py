"""Explicit stopping verifies owned resources while retaining resumable records."""
import unittest
from unittest.mock import patch

from workspace_launcher import LaunchError
from tests import test_workspace_launcher as fixtures


class StopTests(unittest.TestCase):
    setUp = fixtures.LauncherTests.setUp

    def spawn(self):
        agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        self.tmux.sessions.add(self.launcher.tmux_name(agent))
        return agent

    def test_exited_record_still_cleans_live_resources_and_keeps_resume_data(self):
        agent = self.spawn()
        ident = agent['agent_id']
        self.store.update_agent(self.ws['id'], ident, last_state='exited', native_session_id='saved-native')
        with patch('workspace_launcher.stop_wrapper_process') as stop:
            result = self.launcher.stop(self.ws['id'], ident)
        self.assertFalse(self.tmux.sessions)
        stop.assert_called_once()
        self.assertTrue(result['stop_confirmed'])
        self.assertEqual(result['native_session_id'], 'saved-native')
        self.assertTrue(self.store.identity_path(ident).exists())
        self.assertIsNotNone(self.store.get_agent(self.ws['id'], ident))
        self.assertFalse(self.launcher._pending)
        self.assertFalse(self.launcher._processes)

    def test_tmux_cleanup_failure_cannot_report_stopped(self):
        agent = self.spawn()
        with patch('workspace_launcher.stop_wrapper_process'), \
                patch.object(self.tmux, 'remove_session', side_effect=RuntimeError('terminal remains')):
            with self.assertRaises(LaunchError):
                self.launcher.stop(self.ws['id'], agent['agent_id'])
        saved = self.store.get_agent(self.ws['id'], agent['agent_id'])
        self.assertEqual(saved['last_state'], 'unknown')
        self.assertIn('terminal remains', saved['last_error'])
        self.assertTrue(saved['last_launch']['terminated'])
        self.assertTrue(self.tmux.sessions)

    def test_wrapper_cleanup_failure_is_retryable_and_fences_heartbeat(self):
        agent = self.spawn()
        with patch('workspace_launcher.stop_wrapper_process', side_effect=RuntimeError('wrapper remains')):
            with self.assertRaises(LaunchError):
                self.launcher.stop(self.ws['id'], agent['agent_id'])
        self.launcher.on_heartbeat(agent['registry_name'], True, 4242)
        saved = self.store.get_agent(self.ws['id'], agent['agent_id'])
        self.assertEqual(saved['last_state'], 'unknown')
        self.assertTrue(self.store.identity_path(agent['agent_id']).exists())
        with patch('workspace_launcher.stop_wrapper_process'):
            self.assertTrue(self.launcher.stop(self.ws['id'], agent['agent_id'])['stop_confirmed'])

    def test_same_name_new_owner_is_not_deregistered(self):
        agent = self.spawn()
        name = agent['registry_name']
        self.registry.deregister(name)
        other = self.registry.register('kilo', preferred_name=name, allow_reserved=True)
        with patch('workspace_launcher.stop_wrapper_process'):
            self.launcher.stop(self.ws['id'], agent['agent_id'])
        self.assertEqual(self.registry.resolve_token(other['token'])['name'], name)

    def test_owned_wrapper_is_stopped_even_when_heartbeat_pid_is_invalid(self):
        agent = self.spawn()
        process = self.launcher._processes[agent['agent_id']]
        self.store.update_agent(self.ws['id'], agent['agent_id'],
                                last_launch=dict(agent['last_launch'], wrapper_pid=1))
        self.launcher.stop(self.ws['id'], agent['agent_id'])
        self.assertIsNotNone(process.poll())

    def test_changed_launch_nonce_is_rejected_before_cleanup(self):
        agent = self.spawn()
        with patch('workspace_launcher.stop_wrapper_process') as stop:
            with self.assertRaises(LaunchError) as error:
                self.launcher.stop(self.ws['id'], agent['agent_id'], expected_nonce='old-launch')
        self.assertEqual(error.exception.status, 409)
        self.assertTrue(self.tmux.sessions)
        stop.assert_not_called()
        self.assertFalse(self.store.get_agent(self.ws['id'], agent['agent_id'])['last_launch'].get('terminated'))

    def test_queued_ready_callback_cannot_enqueue_after_confirmed_stop(self):
        callbacks = []
        with patch.object(self.launcher, '_background', side_effect=lambda fn, *args: callbacks.append((fn, args))):
            agent = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'literal')
            self.tmux.sessions.add(self.launcher.tmux_name(agent))
            self.launcher.on_heartbeat(agent['registry_name'], ready=True, pid=4242)
        self.assertTrue(callbacks)
        with patch('workspace_launcher.stop_wrapper_process'):
            self.launcher.stop(self.ws['id'], agent['agent_id'], expected_nonce=agent['last_launch']['nonce'])
        before = self.store.get_agent(self.ws['id'], agent['agent_id'])
        queue = self.data / f"{agent['registry_name']}_queue.jsonl"
        self.assertFalse(queue.exists())
        for fn, args in callbacks:
            fn(*args)
        self.assertFalse(queue.exists())
        self.assertEqual(self.store.get_agent(self.ws['id'], agent['agent_id']), before)

    def test_checkpoint_failure_still_releases_resources_and_reports_failure(self):
        agent = self.spawn()
        self.store.update_agent(self.ws['id'], agent['agent_id'], last_state='running')
        with patch('workspace_launcher.stop_wrapper_process'), \
                patch.object(self.launcher, 'checkpoint', side_effect=RuntimeError('checkpoint failed')):
            with self.assertRaises(LaunchError) as error:
                self.launcher.stop(self.ws['id'], agent['agent_id'])
        self.assertIn('checkpoint', error.exception.message)
        self.assertFalse(self.tmux.sessions)
        self.assertEqual(self.store.get_agent(self.ws['id'], agent['agent_id'])['last_state'], 'exited')

    def test_verified_stop_route_requires_auth_and_unchanged_launch(self):
        import asyncio
        import httpx
        import app as app_module
        from fastapi import FastAPI
        agent = self.spawn()
        path = f"/api/workspaces/{self.ws['id']}/agents/{agent['agent_id']}/stop-verified"
        isolated = FastAPI(routes=app_module.app.routes,
                           exception_handlers=app_module.app.exception_handlers)

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated),
                                         base_url='http://localhost') as client:
                self.assertEqual((await client.post(path)).status_code, 403)
                headers = {'X-Session-Token': 'isolated-test-token'}
                for body in ({}, {'expected_nonce': 12}, {'expected_nonce': None, 'extra': True}):
                    response = await client.post(path, headers=headers, json=body)
                    self.assertEqual(response.status_code, 400)
                response = await client.post(path, headers=headers, json={'expected_nonce': 'stale'})
                self.assertEqual(response.status_code, 409)
                self.assertTrue(self.tmux.sessions)
                response = await client.post(path, headers=headers,
                                             json={'expected_nonce': agent['last_launch']['nonce']})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()['stop_confirmed'])
                self.assertFalse(self.tmux.sessions)

        with patch.object(app_module, 'app', isolated), \
                patch.object(app_module, 'session_token', 'isolated-test-token'), \
                patch.object(app_module, 'workspace_launcher', self.launcher), \
                patch('workspace_launcher.stop_wrapper_process'):
            app_module._install_security_middleware('isolated-test-token', {})
            asyncio.run(exercise())
