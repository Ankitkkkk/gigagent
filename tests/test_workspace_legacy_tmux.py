"""Existing terminals survive the agentchattr -> yapp rename."""
import asyncio
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from tests import test_workspace_launcher as fixtures
from cli_workspaces import tmux_target
from orchestration import OrchestratorService
from workspace_launcher import LaunchError, TmuxOps


class LegacyTerminalTests(unittest.TestCase):
    setUp = fixtures.LauncherTests.setUp
    queue = fixtures.LauncherTests.queue

    def legacy_agent(self, *, manager=False):
        if manager:
            ws = self.launcher.configure_orchestrator(self.ws['id'], 'kilo', str(self.proj))
            agent = ws['agents'][0]
        else:
            agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
        self.tmux.sessions.add('agentchattr-' + agent['agent_id'])
        self.launcher.on_heartbeat(agent['registry_name'], True, 4242)
        return self.store.get_agent(self.ws['id'], agent['agent_id'])

    def test_reconcile_keeps_legacy_manager_and_worker_running(self):
        manager = self.legacy_agent(manager=True)
        worker = self.legacy_agent()
        self.launcher.reconcile()
        for agent in (manager, worker):
            self.assertEqual(self.store.get_agent(self.ws['id'], agent['agent_id'])['last_state'],
                             'running')

    def test_ready_heartbeats_restore_legacy_orchestration_without_replaying_startup(self):
        manager = self.legacy_agent(manager=True)
        worker = self.legacy_agent()
        service = OrchestratorService(path=self.data / 'orchestration.json', workspaces=self.store,
            messages=self.messages, registry=self.registry, agents=self.agents, launcher=self.launcher)
        for agent in (manager, worker):
            self.store.mark_exited(agent['registry_name'])
        message = self.messages.add('human', 'Check this bug', channel=self.ws['channel'])
        service.submit(self.ws['id'], message)
        for agent in (manager, worker):
            before = self.queue(agent['registry_name'])
            self.launcher.on_heartbeat(agent['registry_name'], True, 4242)
            self.assertEqual(self.queue(agent['registry_name']), before)
            self.assertEqual(self.store.get_agent(self.ws['id'], agent['agent_id'])['last_state'],
                             'running')
        token = self.store.read_identity(manager['agent_id'])['token']
        pending = service.call(token, 'pending')
        self.assertEqual([row['agent_id'] for row in pending['workers']], [worker['agent_id']])
        routed = service.call(token, 'route', message_id=message['id'], agent_ids=[worker['agent_id']])
        self.assertEqual(routed['status'], 'queued')
        self.assertEqual(self.store.routing_for(self.ws['id'])[message['id']], [worker['agent_id']])

    def test_workspace_view_supplies_existing_terminal_for_attach(self):
        import app
        agent = self.legacy_agent()
        with patch.object(app, 'workspace_store', self.store), \
                patch.object(app, 'workspace_launcher', self.launcher):
            view = app._ws_view(self.store.get(self.ws['id']))
        self.assertEqual(tmux_target(view['agents'][0]), 'agentchattr-' + agent['agent_id'])

    def test_terminal_probes_do_not_block_http_or_workspace_broadcasts(self):
        import app
        self.legacy_agent()

        async def exercise():
            for operation in ('detail', 'list', 'broadcast'):
                with self.subTest(operation=operation):
                    entered, release, finished = (threading.Event() for _ in range(3))
                    delivered = asyncio.Event()
                    def slow_probe(name):
                        entered.set()
                        release.wait(.3)
                        finished.set()
                        return name in self.tmux.sessions
                    async def broadcast(payload):
                        delivered.set()
                    async def invoke():
                        if operation == 'detail':
                            return await app.get_workspace(self.ws['id'])
                        if operation == 'list':
                            return await app.list_workspaces()
                        app._on_workspace_change(self.ws['id'])
                        await delivered.wait()
                    with patch.object(self.tmux, 'has_session', side_effect=slow_probe), \
                            patch.object(app, '_event_loop', asyncio.get_running_loop()), \
                            patch.object(app, '_broadcast', side_effect=broadcast):
                        task = asyncio.create_task(invoke())
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                            self.assertFalse(finished.is_set(), 'Terminal probe blocked the event loop')
                        finally:
                            release.set()
                            await task

        with patch.object(app, 'workspace_store', self.store), \
                patch.object(app, 'workspace_launcher', self.launcher):
            asyncio.run(exercise())

    def test_resume_refuses_live_legacy_terminal_even_if_saved_exited(self):
        agent = self.legacy_agent()
        self.store.mark_exited(agent['registry_name'])
        with self.assertRaises(LaunchError) as error:
            self.launcher.resume(self.ws['id'], agent['agent_id'], fresh=True)
        self.assertEqual(error.exception.status, 409)
        self.assertIn('already running', str(error.exception))
        self.assertEqual(len(self.popen_calls), 1)

    def test_stop_and_remove_clean_legacy_terminals(self):
        for operation in ('stop', 'remove'):
            with self.subTest(operation=operation):
                agent = self.legacy_agent()
                legacy = 'agentchattr-' + agent['agent_id']
                neighbor = legacy + '-other'
                self.tmux.sessions.add(neighbor)
                result = getattr(self.launcher, operation)(self.ws['id'], agent['agent_id'])
                self.assertNotIn(legacy, self.tmux.sessions)
                self.assertIn(neighbor, self.tmux.sessions)
                if operation == 'stop':
                    self.assertTrue(result['stop_confirmed'])
                    fresh = self.launcher.resume(self.ws['id'], agent['agent_id'], fresh=True)
                    command = self.popen_calls[-1][0]
                    self.assertEqual(command[command.index('--tmux-name') + 1],
                                     'yapp-' + fresh['agent_id'])

    def test_supervision_requires_verified_absence_of_both_prefixes(self):
        agent = self.legacy_agent(manager=True)
        self.store.mark_exited(agent['registry_name'])
        self.tmux.sessions.clear()
        self.launcher._processes[agent['agent_id']].returncode = 0
        # Current name is absent, but legacy lookup failed/was uncertain.
        self.tmux.session_absent = lambda name: name.startswith('yapp-')
        with patch.object(self.launcher, '_resume') as resume:
            self.launcher.tick()
        resume.assert_not_called()
        self.assertEqual(self.store.get(self.ws['id'])['orchestrator']['retry_count'], 0)

    @unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'pidfd_open'),
                         'requires Linux process ownership verification')
    def test_stop_finds_legacy_orphan_wrapper_after_terminal_disappears(self):
        from tests._cli_server import stop_process
        agent = self.legacy_agent()
        root = Path(self.tmp) / 'inert-wrapper'
        root.mkdir()
        script = root / 'wrapper.py'
        script.write_text("import time\nprint('ready', flush=True)\ntime.sleep(30)\n")
        child = None
        self.addCleanup(lambda: stop_process(child) if child is not None else None)
        child = subprocess.Popen([sys.executable, str(script), 'kilo', '--identity-file',
                                  str(self.store.identity_path(agent['agent_id'])), '--tmux-name',
                                  'agentchattr-' + agent['agent_id']], stdout=subprocess.PIPE)
        self.addCleanup(child.stdout.close)
        self.assertTrue(select.select([child.stdout], [], [], 5)[0])
        self.assertEqual(child.stdout.readline(), b'ready\n')
        self.launcher.root = root
        self.launcher._processes.pop(agent['agent_id'])
        self.tmux.sessions.clear()
        self.store.update_agent(self.ws['id'], agent['agent_id'], last_state='exited',
                                last_launch=dict(agent['last_launch'], wrapper_pid=child.pid))
        result = self.launcher.stop(self.ws['id'], agent['agent_id'])
        self.assertTrue(result['stop_confirmed'])
        self.assertIsNotNone(child.poll(), 'Stop must not leave the legacy wrapper alive')


@unittest.skipUnless(shutil.which('tmux') and sys.platform != 'win32', 'requires Unix tmux')
class RealLegacyTerminalTests(unittest.TestCase):
    setUp = fixtures.LauncherTests.setUp

    def test_exact_legacy_lookup_and_cleanup_on_private_tmux_server(self):
        from tests._cli_server import isolated_environment
        env = isolated_environment(self.tmp)
        self.addCleanup(subprocess.run, ['tmux', 'kill-server'], env=env,
                        capture_output=True, timeout=5)
        with patch.dict(os.environ, env, clear=True):
            agent = self.launcher.spawn(self.ws['id'], 'kilo', str(self.proj), 'none')
            legacy = 'agentchattr-' + agent['agent_id']
            neighbor = legacy + '-other'
            self.launcher._tmux = TmuxOps()
            for name in (legacy, neighbor):
                subprocess.run(['tmux', '-f', '/dev/null', 'new-session', '-d', '-s', name,
                                '/bin/sleep', '30'], env=env, capture_output=True, check=True, timeout=5)
            self.launcher.on_heartbeat(agent['registry_name'], True, 4242)
            self.launcher.reconcile()
            self.assertEqual(self.store.get_agent(self.ws['id'], agent['agent_id'])['last_state'],
                             'running')
            self.assertEqual(self.launcher.tmux_name(agent), legacy)
            self.launcher.stop(self.ws['id'], agent['agent_id'])
            self.assertFalse(self.launcher._tmux.has_session(legacy))
            self.assertTrue(self.launcher._tmux.has_session(neighbor))


if __name__ == '__main__':
    unittest.main()
