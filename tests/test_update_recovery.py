"""Concurrent and partial updates, with real orchestration and inert pip calls."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import updates
from cli_tui_update import AutoUpdater
from tests.test_cli_tui_update import AVAILABLE, Clock, FakeHost, run_until_cancelled


class UpdateRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.version_file = self.data / 'VERSION'
        self.version_file.write_text('0.5.0')
        self.commands = []
        self.fail_second = False
        self.patcher = patch.object(updates, 'installed_version', self.version)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def version(self):
        return self.version_file.read_text().strip()

    def runner(self, command, **kwargs):
        self.assertTrue((self.data / updates.LOCK_FILE).exists())
        self.commands.append(command)
        if '-I' in command:
            return SimpleNamespace(returncode=0, stdout=self.version(), stderr='')
        if '--upgrade' in command:
            self.version_file.write_text('0.6.0')
        if '--force-reinstall' in command and self.fail_second:
            return SimpleNamespace(returncode=1, stdout='', stderr='No space left')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def apply(self, release=AVAILABLE, **kwargs):
        return updates.apply(release, method=kwargs.get('method', 'installer'),
                             data_dir=self.data, runner=self.runner, environ={})

    def auto(self, host, *, result=AVAILABLE, clock=None):
        return AutoUpdater(host, data_dir=self.data, config={},
                           check=lambda **kwargs: result, apply=self.apply,
                           install_method=lambda: 'installer', installed_version=self.version,
                           running_version='0.5.0', sleep=clock or Clock(limit=3))

    async def test_stale_release_cannot_downgrade_after_waiting_for_idle(self):
        host = FakeHost()
        host.safe = False
        def other_window_updates(_):
            self.version_file.write_text('0.7.0')
            host.safe = True
        auto = self.auto(host, clock=Clock(hook=other_window_updates))
        await auto._handle(AVAILABLE)
        self.assertEqual(self.version(), '0.7.0')
        self.assertEqual(self.commands, [])
        self.assertEqual(host.relaunched, [('0.7.0', None)])

    async def test_already_installed_version_does_not_reinstall(self):
        self.version_file.write_text('0.6.0')
        outcome = self.apply()
        self.assertTrue(outcome['ok'])
        self.assertEqual(outcome['version'], '0.6.0')
        self.assertEqual(self.commands, [])

    async def test_partial_failure_keeps_both_windows_running_until_repair(self):
        self.fail_second = True
        host = FakeHost()
        await run_until_cancelled(self.auto(host))
        self.assertEqual(self.version(), '0.6.0')  # First pip updated files.
        self.assertEqual(host.relaunched, [])
        self.assertEqual(host.restarted, 0)
        self.assertTrue(any('failed' in notice for notice in host.notices))
        self.assertFalse((self.data / updates.LOCK_FILE).exists())

        peer = FakeHost()
        await self.auto(peer)._relaunch_if_installed_elsewhere()
        self.assertEqual(peer.relaunched, [])

        self.fail_second = False
        self.commands.clear()
        repaired = self.apply()
        self.assertTrue(repaired['ok'])
        self.assertTrue(any('--force-reinstall' in cmd for cmd in self.commands))
        await self.auto(peer)._relaunch_if_installed_elsewhere()
        self.assertEqual(peer.relaunched, [('0.6.0', None)])

    async def test_stale_release_cannot_downgrade_incomplete_newer_install(self):
        self.fail_second = True
        self.assertFalse(self.apply()['ok'])
        self.commands.clear()
        self.version_file.write_text('0.7.0')
        outcome = self.apply()
        self.assertFalse(outcome['ok'])
        self.assertEqual(self.version(), '0.7.0')
        self.assertEqual(self.commands, [])

    async def test_external_relaunch_rechecks_install_state_after_waiting(self):
        self.version_file.write_text('0.6.0')
        for state in ('locked', 'failed'):
            with self.subTest(state=state):
                host = FakeHost()
                host.safe = False
                def intervening_install(_):
                    if state == 'locked':
                        (self.data / updates.LOCK_FILE).write_text('another updater')
                    else:
                        self.fail_second = True
                        self.version_file.write_text('0.5.0')
                        self.assertFalse(self.apply()['ok'])
                    host.safe = True
                auto = self.auto(host, clock=Clock(hook=intervening_install))
                try:
                    await auto._relaunch_if_installed_elsewhere()
                    self.assertEqual(host.relaunched, [])
                finally:
                    (self.data / updates.LOCK_FILE).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
