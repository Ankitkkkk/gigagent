import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from workspace_processes import stop_wrapper_process
from workspace_launcher import LaunchError, TmuxOps
from tests import test_workspace_launcher as launcher_tests


class RemovalTests(unittest.TestCase):
    setUp = launcher_tests.LauncherTests.setUp

    def test_remove_stopped_row_cleans_owned_resources_and_preserves_chat(self):
        agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        ident = agent['agent_id']
        name = agent['registry_name']
        self.store.update_agent(self.ws['id'], ident, last_state='exited')
        self.tmux.sessions.add(self.launcher.tmux_name(agent))
        self.messages.add('user', 'keep this', channel=self.ws['channel'])
        queue = self.data / f'{name}_queue.jsonl'
        queue.write_text('stale queued trigger')
        with patch('workspace_launcher.stop_wrapper_process') as stop:
            result = self.launcher.remove(self.ws['id'], ident)
        stop.assert_called_once()
        self.assertFalse(result['agents'])
        self.assertFalse(self.tmux.sessions)
        self.assertFalse(self.store.identity_path(ident).exists())
        self.assertFalse(queue.exists())
        self.assertFalse(self.registry.is_registered(name))
        self.assertNotIn(ident, self.launcher._pending)
        self.assertEqual(self.messages.get_recent(1, channel=self.ws['channel'])[0]['text'], 'keep this')
        self.assertFalse(self.store.get(self.ws['id'])['agents'])

    def test_failed_terminal_cleanup_keeps_agent_and_identity_for_retry(self):
        agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        with patch('workspace_launcher.stop_wrapper_process'), \
                patch.object(self.tmux, 'remove_session', side_effect=RuntimeError('tmux failed')):
            with self.assertRaises(LaunchError):
                self.launcher.remove(self.ws['id'], agent['agent_id'])
        self.assertIsNotNone(self.store.get_agent(self.ws['id'], agent['agent_id']))
        self.assertTrue(self.store.identity_path(agent['agent_id']).exists())

    def test_stale_row_does_not_deregister_new_owner_of_same_name(self):
        agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        name = agent['registry_name']
        self.registry.deregister(name)
        new = self.registry.register('kilo', preferred_name=name, allow_reserved=True)
        with patch('workspace_launcher.stop_wrapper_process'):
            self.launcher.remove(self.ws['id'], agent['agent_id'])
        self.assertEqual(self.registry.resolve_token(new['token'])['name'], name)

    def test_running_removal_prevents_late_delivery_or_readiness_resurrection(self):
        agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'literal')
        ident = agent['agent_id']
        nonce = agent['last_launch']['nonce']
        self.store.update_agent(self.ws['id'], ident, last_state='running')
        self.tmux.sessions.add(self.launcher.tmux_name(agent))
        with patch('workspace_launcher.stop_wrapper_process'):
            self.launcher.remove(self.ws['id'], ident)
        self.launcher._after_ready(self.ws['id'], ident, nonce)
        self.launcher.on_heartbeat(agent['registry_name'], True, 4242)
        self.clock.sleep(100)
        self.launcher.tick()
        self.assertIsNone(self.store.get_agent(self.ws['id'], ident))
        self.assertFalse((self.data / (agent['registry_name'] + '_queue.jsonl')).exists())
        self.assertFalse(self.kills)

    def test_wrapper_cleanup_failure_keeps_terminal_and_row(self):
        agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        self.tmux.sessions.add(self.launcher.tmux_name(agent))
        with patch('workspace_launcher.stop_wrapper_process', side_effect=RuntimeError('still alive')):
            with self.assertRaises(LaunchError):
                self.launcher.remove(self.ws['id'], agent['agent_id'])
        self.assertTrue(self.tmux.sessions)
        self.assertIsNotNone(self.store.get_agent(self.ws['id'], agent['agent_id']))


    def test_failed_store_save_keeps_row_for_successful_retry(self):
        agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        ident = agent['agent_id']
        with patch('workspace_launcher.stop_wrapper_process'):
            with patch.object(self.store, '_save', side_effect=OSError('disk full')):
                with self.assertRaises(LaunchError):
                    self.launcher.remove(self.ws['id'], ident)
            self.assertIsNotNone(self.store.get_agent(self.ws['id'], ident))
            result = self.launcher.remove(self.ws['id'], ident)
        self.assertEqual(result['agents'], [])

    def test_remove_preserves_survivor_registry_and_saved_identity(self):
        first = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        second = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        with patch('workspace_launcher.stop_wrapper_process'):
            result = self.launcher.remove(self.ws['id'], second['agent_id'])
        survivor = result['agents'][0]
        token = self.store.read_identity(first['agent_id'])['token']
        self.assertEqual(survivor['registry_name'], self.registry.resolve_token(token)['name'])
        self.assertEqual(survivor['registry_name'], first['registry_name'])

    def test_remove_waits_for_spawn_to_publish_owned_process(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        entered, release, removing = threading.Event(), threading.Event(), threading.Event()
        original = self.launcher._launch
        def delayed(*args):
            entered.set()
            if not release.wait(3): raise RuntimeError('test release timed out')
            return original(*args)
        def remove(ident):
            removing.set()
            return self.launcher.remove(self.ws['id'], ident)
        with ThreadPoolExecutor(max_workers=2) as executor, \
                patch.object(self.launcher, '_launch', side_effect=delayed), \
                patch('workspace_launcher.stop_wrapper_process') as stop:
            spawned = executor.submit(self.launcher.spawn, self.ws['id'], 'kilo', str(self.proj), 'none')
            try:
                self.assertTrue(entered.wait(2))
                ident = self.store.get(self.ws['id'])['agents'][0]['agent_id']
                removed = executor.submit(remove, ident)
                self.assertTrue(removing.wait(2))
                # Spawn owns the lifecycle until its process handle and PID exist.
                self.assertFalse(removed.done())
            finally:
                release.set()
            self.assertIsNotNone(spawned.result(timeout=3))
            self.assertFalse(removed.result(timeout=3)['agents'])
        self.assertEqual(stop.call_args.args[0], 4242)
        self.assertFalse(self.launcher._pending)
        self.assertFalse(self.launcher._processes)

    def test_remove_route_authentication_success_and_cleanup_failure(self):
        import asyncio
        import httpx
        import app as app_module
        from fastapi import FastAPI
        from unittest.mock import AsyncMock
        agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        path = f"/api/workspaces/{self.ws['id']}/agents/{agent['agent_id']}/remove"
        isolated = FastAPI(routes=app_module.app.routes,
                           exception_handlers=app_module.app.exception_handlers)
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated),
                                         base_url='http://localhost') as client:
                response = await client.post(path)
                self.assertEqual(response.status_code, 403)
                self.assertIsNotNone(self.store.get_agent(self.ws['id'], agent['agent_id']))
                headers = {'X-Session-Token': 'isolated-test-token'}
                with patch.object(self.tmux, 'remove_session', side_effect=RuntimeError('failed')):
                    response = await client.post(path, headers=headers)
                    self.assertEqual(response.status_code, 409)
                response = await client.post(path, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['agents'], [])
                self.assertEqual(response.json()['id'], self.ws['id'])
                response = await client.post(path, headers=headers)
                self.assertEqual(response.status_code, 404)
        with patch.object(app_module, 'app', isolated), \
                patch.object(app_module, 'session_token', 'isolated-test-token'), \
                patch.object(app_module, 'workspace_store', self.store), \
                patch.object(app_module, 'workspace_launcher', self.launcher), \
                patch.object(app_module, 'registry', self.registry), \
                patch.object(app_module, 'broadcast_status', new_callable=AsyncMock), \
                patch('mcp_bridge.purge_identity') as purge, \
                patch('workspace_launcher.stop_wrapper_process'):
            app_module._install_security_middleware('isolated-test-token', {})
            asyncio.run(exercise())
        purge.assert_called_once_with(agent['registry_name'])


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'pidfd_open'), 'Linux orphan ownership')
class ProcessOwnershipTests(unittest.TestCase):
    def test_reused_pid_is_not_signalled(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(self.stop_child, child)
        stop_wrapper_process(child.pid, Path('/not-this-project'), Path('/identity.json'), 'agentchattr-ag_x')
        self.assertIsNone(child.poll())

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)

    def test_verified_orphan_wrapper_is_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'wrapper.py'
            script.write_text('import time\ntime.sleep(30)\n')
            identity = root / 'identity.json'
            child = subprocess.Popen([sys.executable, str(script), 'fake', '--identity-file',
                                      str(identity), '--tmux-name', 'agentchattr-ag_x'])
            self.addCleanup(self.stop_child, child)
            stop_wrapper_process(child.pid, root, identity, 'agentchattr-ag_x')
            self.assertIsNotNone(child.wait(timeout=5))

    def test_empty_cmdline_on_live_process_fails_closed(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(self.stop_child, child)
        with patch.object(Path, 'read_bytes', return_value=b''):
            with self.assertRaisesRegex(RuntimeError, 'Cannot verify'):
                stop_wrapper_process(child.pid, Path('/root'), Path('/identity'), 'agentchattr-ag_x')
        self.assertIsNone(child.poll())

    def test_transient_empty_cmdline_is_retried_before_stopping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'wrapper.py'
            script.write_text('import time; time.sleep(30)')
            identity = root / 'identity.json'
            argv = [sys.executable, str(script), 'fake', '--identity-file', str(identity),
                    '--tmux-name', 'agentchattr-ag_x']
            child = subprocess.Popen(argv)
            self.addCleanup(self.stop_child, child)
            with patch.object(Path, 'read_bytes', side_effect=[b'', b'\0'.join(os.fsencode(a) for a in argv)]):
                stop_wrapper_process(child.pid, root, identity, 'agentchattr-ag_x')
            self.assertIsNotNone(child.wait(timeout=5))

    def test_owned_wrapper_ignoring_term_is_killed(self):
        child = subprocess.Popen([sys.executable, '-u', '-c',
            'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready"); time.sleep(30)'],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_child, child)
        self.addCleanup(child.stdout.close)
        import select
        self.assertTrue(select.select([child.stdout], [], [], 3)[0])
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        stop_wrapper_process(child.pid, Path('/unused'), Path('/unused'), 'unused', owned_process=child)
        self.assertIsNotNone(child.poll())


class ExactTmuxTests(unittest.TestCase):
    def test_remove_uses_exact_target_and_checks_absence(self):
        replies = [subprocess.CompletedProcess([], 0, stderr=''),
                   subprocess.CompletedProcess([], 0, stderr=''),
                   subprocess.CompletedProcess([], 1, stderr="can't find session: agentchattr-ag_one")]
        with patch('workspace_launcher.subprocess.run', side_effect=replies) as run:
            TmuxOps().remove_session('agentchattr-ag_one')
        self.assertTrue(all('=agentchattr-ag_one' in call.args[0] for call in run.call_args_list))

    def test_tmux_error_is_not_treated_as_absence(self):
        with patch('workspace_launcher.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 1, stderr='permission denied')):
            with self.assertRaises(RuntimeError): TmuxOps().remove_session('agentchattr-ag_one')
