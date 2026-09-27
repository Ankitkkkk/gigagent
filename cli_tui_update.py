"""Automatic updates for a running TUI.

The host (TuiApplication) provides: notice(text), update_safe() -> bool,
async confirm(text, default=, escape=) -> bool, async restart_server_for_update()
-> str | None, and async relaunch_for_update(version, restart_problem).
"""

import asyncio

import updates
from cli_view_contracts import ActionOutcome


class AutoUpdater:
    def __init__(self, host, *, data_dir, config, check=updates.check, apply=updates.apply,
                 install_method=updates.install_method, installed_version=updates.installed_version,
                 running_version=updates.RUNNING_VERSION, sleep=asyncio.sleep,
                 check_interval=updates.CHECK_TTL, poll_interval=60, idle_interval=2):
        self.host, self.data_dir, self.config = host, data_dir, config
        self._check, self._apply = check, apply
        self._install_method, self._installed_version = install_method, installed_version
        self.running_version = running_version
        self._sleep = sleep
        self.check_interval, self.poll_interval, self.idle_interval = check_interval, poll_interval, idle_interval
        self._announced, self._failed = set(), set()
        self._busy = False
        self._relaunching = False
        self._last_error = None

    async def run(self):
        """Check now and every check_interval; poll for installs by other processes."""
        since_check = self.check_interval
        while not self._relaunching:
            try:
                if since_check >= self.check_interval:
                    since_check = 0
                    result = await asyncio.to_thread(
                        self._check, data_dir=self.data_dir, config=self.config)
                    await self._handle(result)
                await self._relaunch_if_installed_elsewhere()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # Never let update problems stop the TUI.
                message = f'Update check failed ({type(error).__name__}); will retry later.'
                if message != self._last_error:
                    self._last_error = message
                    self.host.notice(message)
            if self._relaunching:
                return
            await self._sleep(self.poll_interval)
            since_check += self.poll_interval

    async def _handle(self, result):
        if result.get('state') != 'update_available' or result['tag'] in self._failed:
            return
        method = self._install_method()
        if method not in ('installer', 'pipx') or not updates.auto_enabled(self.config):
            if result['tag'] not in self._announced:
                self._announced.add(result['tag'])
                self.host.notice(f"yapp {result['latest']} is available. F4 → Update yapp")
            return
        await self._install(result, method, wait_safe=True)

    async def _wait_safe(self):
        while not self.host.update_safe():
            await self._sleep(self.idle_interval)

    async def _install(self, result, method, *, wait_safe):
        if self._busy or self._relaunching:
            self.host.notice('A yapp update is already in progress.')
            return ActionOutcome('cancelled')
        self._busy = True
        try:
            if wait_safe:
                await self._wait_safe()
            self.host.notice(f"Updating yapp to {result['latest']}… (agents keep running)")
            outcome = await asyncio.to_thread(
                self._apply, result, method=method, data_dir=self.data_dir)
            if outcome['state'] == 'locked':
                self.host.notice(f"Another yapp window is installing {result['latest']}; "
                                 'this one reopens when it finishes.')
                return ActionOutcome('completed')
            if not outcome['ok']:
                self._failed.add(result['tag'])
                kind = 'Automatic update' if wait_safe else 'Update'
                self.host.notice(f"{kind} to {result['latest']} failed: {outcome['message']} "
                                 'F4 → Update yapp to retry.')
                return ActionOutcome('failed', outcome['message'])
            problem = await self.host.restart_server_for_update()
            if wait_safe:
                # The user may have opened a dialog while installing; do not cut it off.
                await self._wait_safe()
            await self._relaunch(outcome['version'], problem)
            return ActionOutcome('completed')
        finally:
            self._busy = False

    async def _relaunch_if_installed_elsewhere(self):
        installed = self._installed_version()
        if installed and updates.compare(self.running_version, installed) == 'update_available':
            await self._wait_safe()
            await self._relaunch(installed, None)

    async def _relaunch(self, version, problem):
        self._relaunching = True
        await self.host.relaunch_for_update(version, problem)

    async def update_now(self):
        """F4 → Update yapp: explicit, so it checks even when checks are turned off."""
        result = await asyncio.to_thread(
            self._check, data_dir=self.data_dir, config=self.config, force=True, explicit=True)
        if result['state'] == 'current':
            self.host.notice(f"yapp {result['current']} is up to date.")
            return ActionOutcome('completed')
        if result['state'] != 'update_available':
            self.host.notice(f"Could not check for updates: {result['error']}")
            return ActionOutcome('failed', result['error'])
        method = self._install_method()
        if method not in ('installer', 'pipx'):
            self.host.notice(updates.manual_instructions(method))
            return ActionOutcome('failed')
        accepted = await self.host.confirm(
            f"Update yapp {result['current']} → {result['latest']}? [y/N]\n{result['url']}\n"
            'Installs the update, restarts the server, and reopens yapp.\n'
            'Agents keep running and drafts are kept.', default=False, escape=False)
        if not accepted:
            return ActionOutcome('cancelled')
        self._failed.discard(result['tag'])
        return await self._install(result, method, wait_safe=False)
