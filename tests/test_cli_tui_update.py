"""Automatic update orchestration inside the TUI, with a fake host."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import updates
from cli_tui_update import AutoUpdater

AVAILABLE = {'state': 'update_available', 'current': '0.5.0', 'latest': '0.6.0', 'tag': 'v0.6.0',
             'url': 'https://github.com/Ankitkkkk/yapp/releases/tag/v0.6.0',
             'archive_url': 'https://example.invalid/v0.6.0.zip', 'error': ''}
INSTALLED = {'ok': True, 'state': 'installed', 'version': '0.6.0', 'message': 'Installed yapp 0.6.0.'}


class FakeHost:
    def __init__(self):
        self.notices = []
        self.safe = True
        self.safe_checks = 0
        self.confirm_answer = True
        self.restart_problem = None
        self.restarted = 0
        self.relaunched = []

    def notice(self, text):
        self.notices.append(text)

    def update_safe(self):
        self.safe_checks += 1
        return self.safe

    async def confirm(self, text, *, default=False, escape=False):
        self.confirm_text = text
        return self.confirm_answer

    async def restart_server_for_update(self):
        self.restarted += 1
        return self.restart_problem

    async def relaunch_for_update(self, version, restart_problem=None):
        self.relaunched.append((version, restart_problem))


class Clock:
    """Fake sleep: records waits and runs an optional hook each time."""
    def __init__(self, hook=None, limit=50):
        self.waits, self.hook, self.limit = [], hook, limit

    async def __call__(self, seconds):
        self.waits.append(seconds)
        if self.hook:
            self.hook(len(self.waits))
        if len(self.waits) >= self.limit:
            raise asyncio.CancelledError


def updater(host, *, result=AVAILABLE, applied=INSTALLED, method='installer', config=None,
            installed=lambda: '0.5.0', clock=None, data_dir='/tmp/d'):
    check = Mock(return_value=result)
    apply = Mock(return_value=applied)
    auto = AutoUpdater(host, data_dir=data_dir, config=config or {}, check=check, apply=apply,
                       install_method=lambda: method, installed_version=installed,
                       running_version='0.5.0', sleep=clock or Clock(limit=3),
                       check_interval=100, poll_interval=10, idle_interval=1)
    return auto, check, apply


async def run_until_cancelled(auto):
    try:
        await auto.run()
    except asyncio.CancelledError:
        pass


class AutoUpdaterTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_update_installs_restarts_and_relaunches(self):
        host = FakeHost()
        auto, check, apply = updater(host)
        await run_until_cancelled(auto)
        apply.assert_called_once()
        self.assertEqual(host.restarted, 1)
        self.assertEqual(host.relaunched, [('0.6.0', None)])
        self.assertIn('Updating yapp to 0.6.0… (agents keep running)', host.notices)

    async def test_waits_until_safe(self):
        host = FakeHost()
        host.safe = False
        clock = Clock(hook=lambda n: setattr(host, 'safe', n >= 3), limit=10)
        auto, _, apply = updater(host, clock=clock)
        await run_until_cancelled(auto)
        self.assertEqual(clock.waits[:3], [1, 1, 1])
        apply.assert_called_once()
        self.assertEqual(len(host.relaunched), 1)

    async def test_waits_for_safe_again_before_relaunching(self):
        host = FakeHost()
        clock = Clock(hook=lambda n: setattr(host, 'safe', n >= 2), limit=10)
        auto, _, apply = updater(host, clock=clock)
        async def restart():
            host.safe = False  # The user opened a dialog while the update installed.
            return None
        host.restart_server_for_update = restart
        await run_until_cancelled(auto)
        self.assertEqual(clock.waits[:2], [1, 1])
        self.assertEqual(host.relaunched, [('0.6.0', None)])

    async def test_restart_waits_until_safe_again_after_install(self):
        host = FakeHost()
        events = []
        clock = Clock(hook=lambda n: (events.append(('tick', host.restarted)),
                                      setattr(host, 'safe', n >= 3)), limit=10)
        auto, _, apply = updater(host, clock=clock)
        def install(*args, **kwargs):
            host.safe = False  # The user started work while apply ran.
            return INSTALLED
        apply.side_effect = install
        await run_until_cancelled(auto)
        self.assertEqual(clock.waits[:3], [1, 1, 1])
        self.assertEqual(events[:3], [('tick', 0)] * 3)
        self.assertEqual(host.restarted, 1)
        self.assertEqual(host.relaunched, [('0.6.0', None)])

    async def test_failed_install_is_reported_once_and_not_retried(self):
        host = FakeHost()
        failed = {'ok': False, 'state': 'failed', 'version': '', 'message': 'Install failed: boom.'}
        clock = Clock(limit=25)
        auto, check, apply = updater(host, applied=failed, clock=clock)
        await run_until_cancelled(auto)
        apply.assert_called_once()
        self.assertGreater(check.call_count, 1)
        self.assertEqual(host.relaunched, [])
        self.assertEqual(sum('Automatic update to 0.6.0 failed' in n for n in host.notices), 1)
        self.assertTrue(any('F4 → Update yapp' in n for n in host.notices))

    async def test_restart_problem_is_carried_to_relaunch_notice(self):
        host = FakeHost()
        host.restart_problem = 'Server did not come back.'
        auto, _, _ = updater(host)
        await run_until_cancelled(auto)
        self.assertEqual(host.relaunched, [('0.6.0', 'Server did not come back.')])

    async def test_auto_off_only_notices_once(self):
        host = FakeHost()
        clock = Clock(limit=25)
        auto, _, apply = updater(host, config={'updates': {'auto': False}}, clock=clock)
        await run_until_cancelled(auto)
        apply.assert_not_called()
        self.assertEqual(host.notices.count('yapp 0.6.0 is available. F4 → Update yapp'), 1)

    async def test_checkout_install_never_auto_updates(self):
        host = FakeHost()
        auto, _, apply = updater(host, method='checkout')
        await run_until_cancelled(auto)
        apply.assert_not_called()
        self.assertIn('yapp 0.6.0 is available. F4 → Update yapp', host.notices)

    async def test_nothing_to_do_when_current_or_unknown(self):
        for state in ('current', 'unknown', 'disabled'):
            with self.subTest(state=state):
                host = FakeHost()
                auto, _, apply = updater(host, result=dict(AVAILABLE, state=state))
                await run_until_cancelled(auto)
                apply.assert_not_called()
                self.assertEqual(host.notices, [])

    async def test_locked_update_waits_then_relaunches_on_version_change(self):
        host = FakeHost()
        version = ['0.5.0']
        locked = {'ok': False, 'state': 'locked', 'version': '',
                  'message': 'Another yapp update is already running.'}
        clock = Clock(hook=lambda n: version.__setitem__(0, '0.6.0') if n == 2 else None, limit=10)
        auto, _, apply = updater(host, applied=locked, installed=lambda: version[0], clock=clock)
        await run_until_cancelled(auto)
        self.assertEqual(host.restarted, 0)
        self.assertEqual(host.relaunched, [('0.6.0', None)])

    async def test_external_update_triggers_relaunch(self):
        host = FakeHost()
        auto, _, apply = updater(host, result=dict(AVAILABLE, state='current'),
                                 installed=lambda: '0.6.0')
        await run_until_cancelled(auto)
        apply.assert_not_called()
        self.assertEqual(host.relaunched, [('0.6.0', None)])

    async def test_external_relaunch_waits_while_the_update_lock_is_held(self):
        # Between the installer's two pip commands the tree is half-installed.
        with tempfile.TemporaryDirectory() as data:
            lock = Path(data) / updates.LOCK_FILE
            lock.write_text('123')
            seen = []
            def tick(n):
                seen.append(list(host.relaunched))
                if n == 3:
                    lock.unlink()
            host = FakeHost()
            clock = Clock(hook=tick, limit=10)
            auto, _, apply = updater(host, result=dict(AVAILABLE, state='current'),
                                     installed=lambda: '0.6.0', clock=clock, data_dir=data)
            await run_until_cancelled(auto)
            apply.assert_not_called()
            self.assertEqual(seen[:3], [[], [], []])
            self.assertEqual(host.relaunched, [('0.6.0', None)])

    async def test_external_relaunch_is_skipped_while_this_window_installs(self):
        host = FakeHost()
        auto, _, _ = updater(host, installed=lambda: '0.6.0')
        auto._busy = True
        await auto._relaunch_if_installed_elsewhere()
        self.assertEqual(host.relaunched, [])
        auto._busy = False
        await auto._relaunch_if_installed_elsewhere()
        self.assertEqual(host.relaunched, [('0.6.0', None)])

    async def test_source_checkout_does_not_relaunch_when_version_changes(self):
        for method in ('checkout', 'unknown'):
            with self.subTest(method=method):
                host = FakeHost()
                auto, _, _ = updater(host, result=dict(AVAILABLE, state='current'), method=method,
                                     installed=lambda: '0.6.0')
                await run_until_cancelled(auto)
                self.assertEqual(host.relaunched, [])

    async def test_installing_is_true_only_while_apply_runs(self):
        host = FakeHost()
        auto, _, apply = updater(host)
        seen = []
        def install(*args, **kwargs):
            seen.append(auto.installing)
            return INSTALLED
        apply.side_effect = install
        self.assertFalse(auto.installing)
        await run_until_cancelled(auto)
        self.assertEqual(seen, [True])
        self.assertFalse(auto.installing)

    async def test_check_errors_never_escape(self):
        host = FakeHost()
        auto, check, _ = updater(host)
        check.side_effect = RuntimeError('boom')
        await run_until_cancelled(auto)
        self.assertEqual(host.relaunched, [])

    async def test_update_now_confirm_paths(self):
        host = FakeHost()
        auto, check, apply = updater(host)
        host.confirm_answer = False
        self.assertEqual((await auto.update_now()).status, 'cancelled')
        apply.assert_not_called()
        self.assertIn('0.5.0 → 0.6.0', host.confirm_text)
        self.assertIn('drafts are kept', host.confirm_text)
        host.confirm_answer = True
        self.assertEqual((await auto.update_now()).status, 'completed')
        check.assert_called_with(data_dir='/tmp/d', config={}, force=True, explicit=True)
        self.assertEqual(host.relaunched, [('0.6.0', None)])

    async def test_update_now_messages(self):
        for result, method, expected in (
                (dict(AVAILABLE, state='current', current='0.6.0'), 'installer', 'yapp 0.6.0 is up to date.'),
                (dict(AVAILABLE, state='unknown', error='Could not reach GitHub'), 'installer', 'Could not reach GitHub'),
                (AVAILABLE, 'checkout', 'git pull')):
            with self.subTest(expected=expected):
                host = FakeHost()
                auto, _, apply = updater(host, result=result, method=method)
                await auto.update_now()
                apply.assert_not_called()
                self.assertIn(expected, host.notices[-1])


if __name__ == '__main__':
    unittest.main()
