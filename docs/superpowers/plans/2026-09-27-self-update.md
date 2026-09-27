# Self-update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Installed copies of yapp detect new GitHub Releases and, inside a running TUI, install them automatically, restart the server, and relaunch with drafts restored; plus a `yapp update` command and an F4 "Update yapp" action.

**Architecture:** A dependency-free `updates.py` owns release checks (cached), install-method detection, the install step, and the relaunch-state file. `cli_tui_update.py` holds an `AutoUpdater` that runs inside the TUI and talks to `TuiApplication` through five small host methods. `cli_update.py` implements `yapp update`. `install.sh` installs the latest release and writes an install marker. The server's `/api/version_check` reuses `updates.check`.

**Tech Stack:** Python 3.11+ standard library (`urllib`, `json`, `subprocess`), prompt_toolkit TUI, FastAPI server, POSIX `sh` installer, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-27-self-update-design.md`

## Global Constraints

- Release source: `https://api.github.com/repos/Ankitkkkk/yapp/releases/latest`; archive URL `https://github.com/Ankitkkkk/yapp/archive/refs/tags/<tag>.zip`.
- No new third-party dependencies; do not import `packaging`.
- Successful check cache: 6 hours. Failed check cache: 1 hour. HTTP timeout: 5 seconds. Install timeout: 10 minutes. Relaunch state max age: 10 minutes.
- Cache file `<data_dir>/update_check.json`; lock file `<data_dir>/update.lock`; relaunch file `<data_dir>/relaunch_state.json` (mode 0600); installer marker `<venv>/yapp-install.json`.
- Opt-outs: `[updates] check = false` / `YAPP_NO_UPDATE_CHECK=1` (no background checks), `[updates] auto = false` / `YAPP_NO_AUTO_UPDATE=1` (notice only). Explicit `yapp update` and F4 → Update yapp always work.
- Test hooks: `YAPP_UPDATE_URL` overrides the API URL; `YAPP_UPDATE_ARCHIVE` overrides the archive to install.
- Never run shell commands with `shell=True`. Shell commands such as `yapp status` never update.
- Run tests with `.venv/bin/python -m unittest ...` from the repository root.

## Review Focus

- The TUI is attached to an agent terminal (F6) or has a dialog open when a release appears → the update waits; it must never tear down a handed-off terminal. (Task 7, `test_waits_until_safe`.)
- GitHub returns 403 rate-limit, 404 (no releases yet), or HTML instead of JSON → state `unknown` with a readable reason, cached for 1 hour, no crash. (Task 1, `test_http_errors_are_unknown_and_cached_briefly`.)
- The install succeeds but the server restart fails or times out → the TUI still relaunches and the new TUI tells the user to restart the server. (Task 7, `test_restart_problem_is_carried_to_relaunch_notice`.)
- A `relaunch_state.json` that is stale, corrupt, or has wrong types → ignored and deleted, never raises. (Task 3, `test_invalid_or_stale_state_is_ignored_and_deleted`.)
- Two TUIs open when a release appears → only one installs; the other sees the lock, then relaunches when the installed `VERSION` changes. (Task 7, `test_locked_update_waits_then_relaunches_on_version_change`.)

---

## File Structure

- Create `updates.py` — release check, cache, opt-outs, install method, apply with lock and verification, relaunch state.
- Create `cli_update.py` — `yapp update` command.
- Create `cli_tui_update.py` — `AutoUpdater` that runs inside the TUI.
- Modify `config_loader.py` — merge `[updates]` from `config.local.toml`.
- Modify `cli_tui_state.py` — `DraftStore.entries()`.
- Modify `cli_workspace_chat.py` — `WorkspaceChatController.busy` property.
- Modify `cli_tui.py` — host methods, updater task, restored drafts, relaunch flag.
- Modify `cli_tui_view.py` — F4 choice `update`.
- Modify `cli_tui_dialogs.py` — dispatch `update`.
- Modify `cli.py` — `update` subcommand, relaunch-state restore, `os.execv` relaunch.
- Modify `app.py` — `/api/version_check` uses `updates.check`; delete old helpers.
- Modify `static/chat.js` — show pill only for `update_available`.
- Modify `install.sh` — install latest release, write marker.
- Modify `README.md`, `INSTALLATION.md`, `config.toml` — docs and commented `[updates]` example.
- Tests: `tests/test_updates.py`, `tests/test_cli_update.py`, `tests/test_cli_tui_update.py`, `tests/test_version_check_api.py`.

---

### Task 1: Release check with cache and opt-outs

**Files:**
- Create: `updates.py`
- Modify: `config_loader.py` (inside `load_config`, after the `[agents]` merge)
- Test: `tests/test_updates.py`

**Interfaces:**
- Produces:
  - `updates.parse_version(text: str) -> tuple[int, ...] | None`
  - `updates.compare(current: str, latest: str) -> str` (`'update_available' | 'current' | 'unknown'`)
  - `updates.installed_version(root: Path = ROOT) -> str`
  - `updates.RUNNING_VERSION: str`
  - `updates.check_enabled(config: dict | None, environ=os.environ) -> bool`
  - `updates.auto_enabled(config: dict | None, environ=os.environ) -> bool`
  - `updates.latest_release(*, opener=urllib.request.urlopen, environ=os.environ) -> dict` with keys `tag, latest, url, archive_url`
  - `updates.check(*, data_dir, config=None, force=False, explicit=False, current=None, opener=urllib.request.urlopen, environ=os.environ, now=time.time) -> dict` with keys `state, current, latest, tag, url, archive_url, error`
  - `updates.data_dir_from_config(config: dict) -> Path`
  - constants `REPO, API_URL, INSTALLER_COMMAND, CHECK_TTL, FAILURE_TTL, CHECK_TIMEOUT, APPLY_TIMEOUT, RELAUNCH_MAX_AGE, CACHE_FILE, LOCK_FILE, RELAUNCH_FILE, MARKER, ROOT`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_updates.py`:

```python
"""Release checks, install methods, apply, and relaunch state."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

import updates


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(payload=None, error=None):
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        if error is not None:
            raise error
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return FakeResponse(body)
    opener.calls = calls
    return opener


RELEASE = {'tag_name': 'v0.6.0', 'html_url': 'https://github.com/Ankitkkkk/yapp/releases/tag/v0.6.0'}


class VersionTests(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(updates.parse_version('v0.6.1'), (0, 6, 1))
        self.assertEqual(updates.parse_version('0.5'), (0, 5))
        self.assertIsNone(updates.parse_version('nightly'))
        self.assertEqual(updates.compare('0.5.0', 'v0.6.0'), 'update_available')
        self.assertEqual(updates.compare('0.6.0', 'v0.6.0'), 'current')
        self.assertEqual(updates.compare('0.6', '0.6.0'), 'current')
        self.assertEqual(updates.compare('0.7.0', 'v0.6.0'), 'current')
        self.assertEqual(updates.compare('', 'v0.6.0'), 'unknown')
        self.assertEqual(updates.compare('0.5.0', 'latest'), 'unknown')

    def test_installed_version_reads_disk(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'VERSION').write_text('1.2.3\n')
            self.assertEqual(updates.installed_version(root), '1.2.3')
            self.assertEqual(updates.installed_version(Path(root, 'missing')), '')


class SettingsTests(unittest.TestCase):
    def test_opt_outs(self):
        self.assertTrue(updates.check_enabled({}, {}))
        self.assertTrue(updates.auto_enabled({}, {}))
        self.assertFalse(updates.check_enabled({'updates': {'check': False}}, {}))
        self.assertFalse(updates.check_enabled({}, {'YAPP_NO_UPDATE_CHECK': '1'}))
        self.assertFalse(updates.auto_enabled({'updates': {'auto': False}}, {}))
        self.assertFalse(updates.auto_enabled({}, {'YAPP_NO_AUTO_UPDATE': 'true'}))
        self.assertFalse(updates.auto_enabled({'updates': {'check': False}}, {}))
        self.assertTrue(updates.check_enabled({'updates': {'auto': False}}, {}))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.clock = [1000.0]

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, **kwargs):
        kwargs.setdefault('current', '0.5.0')
        kwargs.setdefault('environ', {})
        return updates.check(data_dir=self.data, now=lambda: self.clock[0], **kwargs)

    def test_update_available_and_fields(self):
        opener = opener_for(RELEASE)
        result = self.check(opener=opener)
        self.assertEqual(result['state'], 'update_available')
        self.assertEqual(result['latest'], '0.6.0')
        self.assertEqual(result['tag'], 'v0.6.0')
        self.assertEqual(result['archive_url'],
                         'https://github.com/Ankitkkkk/yapp/archive/refs/tags/v0.6.0.zip')
        self.assertEqual(opener.calls, [(updates.API_URL, updates.CHECK_TIMEOUT)])

    def test_current(self):
        self.assertEqual(self.check(opener=opener_for(RELEASE), current='0.6.0')['state'], 'current')

    def test_cache_hit_expiry_and_force(self):
        opener = opener_for(RELEASE)
        self.check(opener=opener)
        self.clock[0] += updates.CHECK_TTL - 1
        self.check(opener=opener)
        self.assertEqual(len(opener.calls), 1)
        self.check(opener=opener, force=True)
        self.assertEqual(len(opener.calls), 2)
        self.clock[0] += updates.CHECK_TTL + 1
        self.check(opener=opener)
        self.assertEqual(len(opener.calls), 3)

    def test_http_errors_are_unknown_and_cached_briefly(self):
        cases = [(urllib.error.HTTPError(updates.API_URL, 403, 'rate', {}, None), 'rate limit'),
                 (urllib.error.HTTPError(updates.API_URL, 404, 'missing', {}, None), 'No yapp release'),
                 (urllib.error.URLError('offline'), 'Could not reach GitHub'),
                 (None, 'Unexpected response')]
        for error, reason in cases:
            with self.subTest(reason=reason):
                (self.data / updates.CACHE_FILE).unlink(missing_ok=True)
                opener = opener_for(b'<html>', error=error)
                result = self.check(opener=opener)
                self.assertEqual(result['state'], 'unknown')
                self.assertIn(reason, result['error'])
                self.clock[0] += updates.FAILURE_TTL - 1
                self.check(opener=opener)
                self.assertEqual(len(opener.calls), 1)
                self.clock[0] += 2
                self.check(opener=opener)
                self.assertEqual(len(opener.calls), 2)

    def test_release_without_version_tag_is_unknown(self):
        result = self.check(opener=opener_for({'tag_name': 'nightly'}))
        self.assertEqual(result['state'], 'unknown')

    def test_disabled_makes_no_request_unless_explicit(self):
        opener = opener_for(RELEASE)
        config = {'updates': {'check': False}}
        self.assertEqual(self.check(opener=opener, config=config)['state'], 'disabled')
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.check(opener=opener, config=config, explicit=True)['state'],
                         'update_available')

    def test_update_url_override(self):
        opener = opener_for(RELEASE)
        self.check(opener=opener, environ={'YAPP_UPDATE_URL': 'http://127.0.0.1:9/release'})
        self.assertEqual(opener.calls[0][0], 'http://127.0.0.1:9/release')

    def test_corrupt_cache_is_refetched(self):
        (self.data / updates.CACHE_FILE).write_text('{not json')
        opener = opener_for(RELEASE)
        self.assertEqual(self.check(opener=opener)['state'], 'update_available')
        self.assertEqual(len(opener.calls), 1)

    def test_data_dir_from_config(self):
        self.assertEqual(updates.data_dir_from_config({'server': {'data_dir': '/abs/data'}}),
                         Path('/abs/data'))
        self.assertEqual(updates.data_dir_from_config({}), updates.ROOT / 'data')


class ConfigMergeTests(unittest.TestCase):
    def test_local_updates_section_is_merged(self):
        from config_loader import load_config
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'config.toml').write_text('[server]\nport = 1\n')
            Path(root, 'config.local.toml').write_text('[updates]\nauto = false\n')
            self.assertEqual(load_config(Path(root))['updates'], {'auto': False})


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_updates -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'updates'`.

- [ ] **Step 3: Implement `updates.py` (check part)**

```python
"""Release checks and self-update for installed copies of yapp.

Standard library only, so it works in every install type.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = 'Ankitkkkk/yapp'
API_URL = f'https://api.github.com/repos/{REPO}/releases/latest'
INSTALLER_COMMAND = 'curl -fsSL https://yapp.riggedcode.com/install.sh | sh'
CHECK_TTL = 6 * 3600
FAILURE_TTL = 3600
CHECK_TIMEOUT = 5
APPLY_TIMEOUT = 600
RELAUNCH_MAX_AGE = 600
CACHE_FILE = 'update_check.json'
LOCK_FILE = 'update.lock'
RELAUNCH_FILE = 'relaunch_state.json'
MARKER = 'yapp-install.json'
_VERSION = re.compile(r'^v?(\d+(?:\.\d+)*)$')


def parse_version(text):
    match = _VERSION.match(str(text or '').strip())
    return tuple(int(part) for part in match.group(1).split('.')) if match else None


def compare(current, latest):
    """Return 'update_available', 'current', or 'unknown'; never guess."""
    a, b = parse_version(current), parse_version(latest)
    if a is None or b is None:
        return 'unknown'
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    return 'update_available' if b > a else 'current'


def installed_version(root=ROOT):
    """VERSION as it is on disk now; differs from RUNNING_VERSION after an update."""
    try:
        return (Path(root) / 'VERSION').read_text().strip()
    except OSError:
        return ''


RUNNING_VERSION = installed_version()


def _flag(environ, name):
    return str(environ.get(name, '')).strip().lower() in ('1', 'true', 'yes', 'on')


def check_enabled(config, environ=os.environ):
    settings = (config or {}).get('updates', {})
    return not _flag(environ, 'YAPP_NO_UPDATE_CHECK') and settings.get('check', True) is not False


def auto_enabled(config, environ=os.environ):
    settings = (config or {}).get('updates', {})
    return (check_enabled(config, environ) and not _flag(environ, 'YAPP_NO_AUTO_UPDATE')
            and settings.get('auto', True) is not False)


def data_dir_from_config(config):
    path = Path((config or {}).get('server', {}).get('data_dir', './data'))
    return path if path.is_absolute() else ROOT / path


def latest_release(*, opener=urllib.request.urlopen, environ=os.environ):
    url = environ.get('YAPP_UPDATE_URL') or API_URL
    request = urllib.request.Request(
        url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'yapp'})
    with opener(request, timeout=CHECK_TIMEOUT) as response:
        data = json.loads(response.read())
    tag = data.get('tag_name') if isinstance(data, dict) else None
    if not isinstance(tag, str) or parse_version(tag) is None:
        raise ValueError('release has no version tag')
    return {'tag': tag, 'latest': tag.lstrip('v'),
            'url': str(data.get('html_url') or f'https://github.com/{REPO}/releases/tag/{tag}'),
            'archive_url': f'https://github.com/{REPO}/archive/refs/tags/{tag}.zip'}


def _reason(error):
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (403, 429):
            return 'GitHub rate limit reached; try again later.'
        if error.code == 404:
            return 'No yapp release has been published yet.'
        return f'GitHub returned HTTP {error.code}.'
    if isinstance(error, (urllib.error.URLError, OSError, TimeoutError)):
        return 'Could not reach GitHub to check for updates.'
    return 'Unexpected response from GitHub.'


def _read_cache(path, now):
    try:
        record = json.loads(path.read_text())
        age = now - float(record['checked_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ttl = FAILURE_TTL if record.get('error') else CHECK_TTL
    return record if 0 <= age < ttl else None


def _write_cache(path, record):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    except OSError:
        pass


def check(*, data_dir, config=None, force=False, explicit=False, current=None,
          opener=urllib.request.urlopen, environ=os.environ, now=time.time):
    current = RUNNING_VERSION if current is None else current
    result = {'state': 'unknown', 'current': current, 'latest': '', 'tag': '',
              'url': '', 'archive_url': '', 'error': ''}
    if not explicit and not check_enabled(config, environ):
        return dict(result, state='disabled')
    cache = Path(data_dir) / CACHE_FILE
    record = None if force else _read_cache(cache, now())
    if record is None:
        try:
            record = dict(latest_release(opener=opener, environ=environ), error='')
        except Exception as error:  # Network, HTTP, JSON, or tag errors all mean "unknown".
            record = {'error': _reason(error)}
        record['checked_at'] = now()
        _write_cache(cache, record)
    if record.get('error'):
        return dict(result, error=record['error'])
    for key in ('latest', 'tag', 'url', 'archive_url'):
        result[key] = str(record.get(key, ''))
    result['state'] = compare(current, result['latest'])
    return result
```

Also in `config_loader.py` `load_config`, directly after the `for name, agent_cfg in local_agents.items():` loop (still inside `if local_path.exists():`), add:

```python
        # [updates] settings (e.g. auto = false) may live in the local file too.
        local_updates = local.get("updates")
        if isinstance(local_updates, dict):
            config.setdefault("updates", {}).update(local_updates)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_updates -v`
Expected: all tests in `VersionTests`, `SettingsTests`, `CheckTests`, `ConfigMergeTests` PASS.

- [ ] **Step 5: Commit**

```bash
git add updates.py config_loader.py tests/test_updates.py
git commit -m "feat: add cached GitHub release check for yapp updates"
```

---

### Task 2: Install method detection and applying an update

**Files:**
- Modify: `updates.py` (append)
- Test: `tests/test_updates.py` (append classes)

**Interfaces:**
- Consumes: Task 1 constants and `check` result dict (`tag`, `latest`, `archive_url`).
- Produces:
  - `updates.install_method(*, prefix=sys.prefix, root=ROOT, which=shutil.which) -> str` (`'installer' | 'pipx' | 'checkout' | 'unknown'`)
  - `updates.manual_instructions(method: str) -> str`
  - `updates.apply(release: dict, *, method: str, data_dir, python=sys.executable, runner=subprocess.run, which=shutil.which, environ=os.environ, now=time.time) -> dict` with keys `ok: bool, state: 'installed'|'failed'|'locked'|'unsupported', message: str, version: str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_updates.py` (before the `if __name__` block):

```python
import subprocess
from types import SimpleNamespace

CHECKED = {'tag': 'v0.6.0', 'latest': '0.6.0',
           'archive_url': 'https://github.com/Ankitkkkk/yapp/archive/refs/tags/v0.6.0.zip'}


class InstallMethodTests(unittest.TestCase):
    def test_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            code, venv = tmp / 'code', tmp / 'venv'
            code.mkdir(); venv.mkdir()
            self.assertEqual(updates.install_method(prefix=venv, root=code, which=lambda _: None), 'unknown')
            (venv / updates.MARKER).write_text('{}')
            self.assertEqual(updates.install_method(prefix=venv, root=code, which=lambda _: None), 'installer')
            (code / 'pyproject.toml').write_text('')
            self.assertEqual(updates.install_method(prefix=venv, root=code, which=lambda _: None), 'checkout')
            pipx = tmp / 'share' / 'pipx' / 'venvs' / 'yapp'
            pipx.mkdir(parents=True)
            code2 = tmp / 'code2'; code2.mkdir()
            self.assertEqual(updates.install_method(prefix=pipx, root=code2, which=lambda _: '/bin/pipx'), 'pipx')
            self.assertEqual(updates.install_method(prefix=pipx, root=code2, which=lambda _: None), 'unknown')

    def test_manual_instructions(self):
        self.assertIn('git pull', updates.manual_instructions('checkout'))
        self.assertIn(updates.INSTALLER_COMMAND, updates.manual_instructions('unknown'))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, version='0.6.0', fail_at=None):
        def run(command, **kwargs):
            self.calls.append(command)
            self.assertNotIn('shell', kwargs)
            if fail_at is not None and len(self.calls) - 1 == fail_at:
                return SimpleNamespace(returncode=1, stdout='', stderr='line1\nERROR: no space left')
            out = version + '\n' if command[1:3] == ['-c', updates._VERSION_PROBE] else ''
            return SimpleNamespace(returncode=0, stdout=out, stderr='')
        return run

    def test_installer_commands_and_verification(self):
        result = updates.apply(CHECKED, method='installer', data_dir=self.data,
                               python='/venv/bin/python', runner=self.runner(), environ={})
        self.assertEqual(result['state'], 'installed')
        self.assertTrue(result['ok'])
        self.assertEqual(result['version'], '0.6.0')
        archive = CHECKED['archive_url']
        self.assertEqual(self.calls[:2], [
            ['/venv/bin/python', '-m', 'pip', 'install', '-q', '--upgrade', archive],
            ['/venv/bin/python', '-m', 'pip', 'install', '-q', '--force-reinstall', '--no-deps', archive]])
        self.assertFalse((self.data / updates.LOCK_FILE).exists())

    def test_pipx_command_and_archive_override(self):
        result = updates.apply(CHECKED, method='pipx', data_dir=self.data, python='/p/bin/python',
                               runner=self.runner(), which=lambda _: '/usr/bin/pipx',
                               environ={'YAPP_UPDATE_ARCHIVE': '/tmp/yapp.zip'})
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls[0], ['/usr/bin/pipx', 'install', '--force', '/tmp/yapp.zip'])

    def test_failure_reports_tail_and_manual_command(self):
        result = updates.apply(CHECKED, method='installer', data_dir=self.data,
                               runner=self.runner(fail_at=0), environ={})
        self.assertEqual(result['state'], 'failed')
        self.assertIn('no space left', result['message'])
        self.assertIn(updates.INSTALLER_COMMAND, result['message'])
        self.assertFalse((self.data / updates.LOCK_FILE).exists())

    def test_version_mismatch_is_failure(self):
        result = updates.apply(CHECKED, method='installer', data_dir=self.data,
                               runner=self.runner(version='0.5.0'), environ={})
        self.assertEqual(result['state'], 'failed')
        self.assertIn('0.5.0', result['message'])

    def test_runner_exception_is_failure(self):
        def boom(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 600)
        result = updates.apply(CHECKED, method='installer', data_dir=self.data, runner=boom, environ={})
        self.assertEqual(result['state'], 'failed')
        self.assertIn('TimeoutExpired', result['message'])

    def test_unsupported_methods(self):
        for method in ('checkout', 'unknown'):
            result = updates.apply(CHECKED, method=method, data_dir=self.data, runner=self.runner())
            self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(self.calls, [])

    def test_lock_prevents_second_update_and_stale_lock_is_replaced(self):
        lock = self.data / updates.LOCK_FILE
        lock.write_text('123')
        result = updates.apply(CHECKED, method='installer', data_dir=self.data,
                               runner=self.runner(), environ={})
        self.assertEqual(result['state'], 'locked')
        self.assertEqual(self.calls, [])
        old = lock.stat().st_mtime - updates.APPLY_TIMEOUT - 120
        os.utime(lock, (old, old))
        result = updates.apply(CHECKED, method='installer', data_dir=self.data,
                               runner=self.runner(), environ={})
        self.assertEqual(result['state'], 'installed')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_updates -v`
Expected: new tests ERROR with `AttributeError: module 'updates' has no attribute 'install_method'`.

- [ ] **Step 3: Implement (append to `updates.py`)**

```python
_VERSION_PROBE = 'from importlib.metadata import version; print(version("yapp"))'


def install_method(*, prefix=sys.prefix, root=ROOT, which=shutil.which):
    if (Path(root) / 'pyproject.toml').exists():
        return 'checkout'
    prefix = Path(prefix)
    if (prefix / MARKER).is_file():
        return 'installer'
    if prefix.resolve().parts[-3:] == ('pipx', 'venvs', 'yapp') and which('pipx'):
        return 'pipx'
    return 'unknown'


def manual_instructions(method):
    if method == 'checkout':
        return 'This is a source checkout: run git pull, then restart yapp.'
    if method == 'unknown':
        return f'Update with: {INSTALLER_COMMAND}'
    return ''


def _acquire_lock(path, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if now() - path.stat().st_mtime > APPLY_TIMEOUT + 60:
                    path.unlink()  # A crashed update left it behind.
                    continue
            except OSError:
                pass
            return None
        with os.fdopen(fd, 'w') as handle:
            handle.write(str(os.getpid()))
        return path
    return None


def _tail(text, lines=3):
    return ' '.join(line.strip() for line in str(text or '').strip().splitlines()[-lines:])


def apply(release, *, method, data_dir, python=sys.executable, runner=subprocess.run,
          which=shutil.which, environ=os.environ, now=time.time):
    """Install `release` for this install method. Never raises for expected failures."""
    if method not in ('installer', 'pipx'):
        return {'ok': False, 'state': 'unsupported', 'version': '',
                'message': manual_instructions(method)}
    archive = environ.get('YAPP_UPDATE_ARCHIVE') or release['archive_url']
    if method == 'installer':
        commands = [[python, '-m', 'pip', 'install', '-q', '--upgrade', archive],
                    [python, '-m', 'pip', 'install', '-q', '--force-reinstall', '--no-deps', archive]]
        manual = f'Update manually with: {INSTALLER_COMMAND}'
    else:
        commands = [[which('pipx') or 'pipx', 'install', '--force', archive]]
        manual = f'Update manually with: pipx install --force {archive}'

    def failed(message):
        return {'ok': False, 'state': 'failed', 'version': '', 'message': f'{message} {manual}'}

    lock = _acquire_lock(Path(data_dir) / LOCK_FILE, now)
    if lock is None:
        return {'ok': False, 'state': 'locked', 'version': '',
                'message': 'Another yapp update is already running.'}
    try:
        for command in commands:
            try:
                result = runner(command, capture_output=True, text=True, timeout=APPLY_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as error:
                return failed(f'{type(error).__name__} while installing yapp {release["latest"]}.')
            if result.returncode:
                return failed(f'Install failed: {_tail(result.stderr or result.stdout)}.')
        try:
            probe = runner([python, '-c', _VERSION_PROBE], capture_output=True, text=True, timeout=60)
            version = probe.stdout.strip() if probe.returncode == 0 else ''
        except (OSError, subprocess.SubprocessError):
            version = ''
        if compare(version, release['latest']) != 'current' or compare(release['latest'], version) != 'current':
            return failed(f'Installed version is {version or "unknown"}, expected {release["latest"]}.')
        return {'ok': True, 'state': 'installed', 'version': version,
                'message': f'Installed yapp {version}.'}
    finally:
        lock.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_updates -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add updates.py tests/test_updates.py
git commit -m "feat: detect install method and apply yapp updates with a lock"
```

---

### Task 3: Relaunch state (drafts survive the relaunch)

**Files:**
- Modify: `updates.py` (append)
- Modify: `cli_tui_state.py` (`DraftStore`)
- Test: `tests/test_updates.py` (append), `tests/test_cli_tui_state.py` (append)

**Interfaces:**
- Produces:
  - `updates.save_relaunch_state(data_dir, *, drafts: list[tuple[tuple[str, str], str, int]], session_id: str | None, channel: str | None, notices: list[str], now=time.time) -> None`
  - `updates.load_relaunch_state(data_dir, *, now=time.time) -> dict | None` → `{'session_id', 'channel', 'drafts': [((kind, name), text, cursor)], 'notices': [str]}`; always deletes the file.
  - `DraftStore.entries() -> Iterator[tuple[tuple, str, int]]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_updates.py`:

```python
class RelaunchStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_private_and_consumed_once(self):
        updates.save_relaunch_state(
            self.data, drafts=[(('session', 'ws_1'), 'hello @claude', 5)],
            session_id='ws_1', channel=None, notices=['Updated to yapp 0.6.0.'], now=lambda: 100.0)
        path = self.data / updates.RELAUNCH_FILE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        state = updates.load_relaunch_state(self.data, now=lambda: 150.0)
        self.assertEqual(state, {'session_id': 'ws_1', 'channel': None,
                                 'drafts': [(('session', 'ws_1'), 'hello @claude', 5)],
                                 'notices': ['Updated to yapp 0.6.0.']})
        self.assertFalse(path.exists())
        self.assertIsNone(updates.load_relaunch_state(self.data, now=lambda: 150.0))

    def test_invalid_or_stale_state_is_ignored_and_deleted(self):
        path = self.data / updates.RELAUNCH_FILE
        payloads = ['{broken',
                    json.dumps({'saved_at': 100, 'drafts': 'nope'}),
                    json.dumps({'saved_at': 100, 'drafts': [{'key': ['session'], 'text': 'x', 'cursor': 0}]}),
                    json.dumps({'saved_at': 'soon', 'drafts': []}),
                    json.dumps({'saved_at': 100 - updates.RELAUNCH_MAX_AGE - 1, 'drafts': []})]
        for payload in payloads:
            with self.subTest(payload=payload):
                path.write_text(payload)
                self.assertIsNone(updates.load_relaunch_state(self.data, now=lambda: 100.0))
                self.assertFalse(path.exists())
```

Append to `tests/test_cli_tui_state.py` a new test class:

```python
class DraftEntriesTests(unittest.TestCase):
    def test_entries_include_cursor(self):
        from cli_tui_state import DraftStore
        drafts = DraftStore()
        drafts.set(('session', 'a'), 'hello', cursor=2)
        drafts.set(('channel', 'general'), 'hi')
        self.assertEqual(list(drafts.entries()),
                         [(('session', 'a'), 'hello', 2), (('channel', 'general'), 'hi', 2)])
```

(If `tests/test_cli_tui_state.py` does not already `import unittest`, add it at the top.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_updates tests.test_cli_tui_state -v`
Expected: ERROR `AttributeError: ... 'save_relaunch_state'` and `'DraftStore' object has no attribute 'entries'`.

- [ ] **Step 3: Implement**

In `cli_tui_state.py`, add to `DraftStore` after `items()`:

```python
    def entries(self):
        """(key, text, cursor) for every unsent draft, oldest first."""
        return ((key, entry[0], entry[1]) for key, entry in self._entries.items())
```

Append to `updates.py`:

```python
def save_relaunch_state(data_dir, *, drafts, session_id, channel, notices, now=time.time):
    payload = {'saved_at': now(), 'session_id': session_id, 'channel': channel,
               'notices': list(notices),
               'drafts': [{'key': list(key), 'text': text, 'cursor': cursor}
                          for key, text, cursor in drafts]}
    path = Path(data_dir) / RELAUNCH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(payload, handle)
    os.replace(temporary, path)


def load_relaunch_state(data_dir, *, now=time.time):
    """Return saved drafts/selection once, or None; the file is always removed."""
    path = Path(data_dir) / RELAUNCH_FILE
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        payload = None
    finally:
        path.unlink(missing_ok=True)
    try:
        age = now() - float(payload['saved_at'])
        if not 0 <= age <= RELAUNCH_MAX_AGE:
            return None
        drafts = []
        for item in payload['drafts']:
            key, text, cursor = item['key'], item['text'], item['cursor']
            if (not isinstance(key, list) or len(key) != 2 or not all(isinstance(k, str) for k in key)
                    or not isinstance(text, str) or not isinstance(cursor, int)):
                return None
            drafts.append(((key[0], key[1]), text, cursor))
        session_id, channel = payload.get('session_id'), payload.get('channel')
        notices = payload.get('notices', [])
        if (not isinstance(session_id, (str, type(None))) or not isinstance(channel, (str, type(None)))
                or not isinstance(notices, list) or not all(isinstance(n, str) for n in notices)):
            return None
    except (TypeError, KeyError, ValueError):
        return None
    return {'session_id': session_id, 'channel': channel, 'drafts': drafts, 'notices': notices}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_updates tests.test_cli_tui_state -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add updates.py cli_tui_state.py tests/test_updates.py tests/test_cli_tui_state.py
git commit -m "feat: save and restore drafts across an update relaunch"
```

---

### Task 4: Installer installs the latest release and writes a marker

**Files:**
- Modify: `install.sh`

**Interfaces:**
- Produces: `$VENV/yapp-install.json` = `{"method": "installer", "source": "<url>", "installed_at": "<UTC ISO>"}` (consumed by `updates.install_method`).

- [ ] **Step 1: Change source resolution**

In `install.sh`, replace the line

```sh
SOURCE="${YAPP_SOURCE:-https://github.com/Ankitkkkk/yapp/archive/refs/heads/main.zip}"
```

with

```sh
MAIN_ARCHIVE="https://github.com/Ankitkkkk/yapp/archive/refs/heads/main.zip"
SOURCE="${YAPP_SOURCE:-}"
```

and directly before `say "Installing yapp from $SOURCE"` insert:

```sh
if [ -z "$SOURCE" ]; then
    # Latest published release; fall back to main when there is none yet.
    TAG="$("$VENV/bin/python" - <<'PY' 2>/dev/null || true
import json, urllib.request
request = urllib.request.Request(
    'https://api.github.com/repos/Ankitkkkk/yapp/releases/latest',
    headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'yapp-installer'})
with urllib.request.urlopen(request, timeout=10) as response:
    tag = json.load(response).get('tag_name', '')
print(tag if isinstance(tag, str) and tag.startswith('v') else '')
PY
)"
    if [ -n "$TAG" ]; then
        SOURCE="https://github.com/Ankitkkkk/yapp/archive/refs/tags/$TAG.zip"
    else
        say "No published release found; installing the latest main branch."
        SOURCE="$MAIN_ARCHIVE"
    fi
fi
```

- [ ] **Step 2: Write the marker after install**

Directly after the line `"$VENV/bin/python" -m pip install -q --force-reinstall --no-deps "$SOURCE"`, add:

```sh
"$VENV/bin/python" - "$VENV/yapp-install.json" "$SOURCE" <<'PY'
import json, sys
from datetime import datetime, timezone
path, source = sys.argv[1], sys.argv[2]
with open(path, 'w') as handle:
    json.dump({'method': 'installer', 'source': source,
               'installed_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}, handle)
PY
```

- [ ] **Step 3: Verify in a scratch home**

Run (from the repository root; `$S` is a scratch directory):

```sh
S=$(mktemp -d); HOME=$S/home YAPP_SOURCE=$PWD PATH=$S/home/.local/bin:/usr/bin:/bin sh install.sh
cat $S/home/.local/share/yapp/venv/yapp-install.json
$S/home/.local/share/yapp/venv/bin/python -c "import yapp.yapp, sys; sys.path.insert(0, str(yapp.yapp.ROOT)); import updates; print(updates.install_method())"
rm -rf build yapp.egg-info
```

Expected: the JSON marker prints with `"method": "installer"`, and the last command prints `installer`.

Also run `sh install.sh` with a fake home and no `YAPP_SOURCE` while no release exists: expected output contains `No published release found; installing the latest main branch.`

- [ ] **Step 4: Commit**

```bash
git add install.sh
git commit -m "feat: installer installs the latest release and records install method"
```

---

### Task 5: Server version check uses `updates.check`

**Files:**
- Modify: `app.py` (replace the block from `# --- Version check (GitHub release notifier) ---` through the end of `async def version_check()`)
- Modify: `static/chat.js` (`checkForUpdate`)
- Test: `tests/test_version_check_api.py`

**Interfaces:**
- Consumes: `updates.check(data_dir=..., config=...)`.
- Produces: `GET /api/version_check` → `{"current", "latest", "state", "url"}`.

- [ ] **Step 1: Write the failing test**

```python
"""The browser update pill reads yapp releases through updates.check."""
import asyncio
import json
import unittest
from unittest.mock import patch

import app


class VersionCheckApiTests(unittest.TestCase):
    def test_uses_updates_check_with_server_data_dir(self):
        result = {'state': 'update_available', 'current': '0.5.0', 'latest': '0.6.0', 'tag': 'v0.6.0',
                  'url': 'https://github.com/Ankitkkkk/yapp/releases/tag/v0.6.0', 'archive_url': 'x', 'error': ''}
        with patch.object(app, 'config', {'server': {'data_dir': '/tmp/yapp-data'}}), \
                patch('updates.check', return_value=result) as check:
            response = asyncio.run(app.version_check())
        check.assert_called_once_with(data_dir='/tmp/yapp-data',
                                      config={'server': {'data_dir': '/tmp/yapp-data'}})
        self.assertEqual(json.loads(response.body), {
            'current': '0.5.0', 'latest': '0.6.0', 'state': 'update_available',
            'url': 'https://github.com/Ankitkkkk/yapp/releases/tag/v0.6.0'})

    def test_old_upstream_helpers_are_gone(self):
        for name in ('_detect_install_kind', '_fetch_latest_release', '_compare_versions'):
            self.assertFalse(hasattr(app, name), name)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_version_check_api -v`
Expected: FAIL (`check` not called / helpers still present).

- [ ] **Step 3: Implement**

Replace the whole version-check block in `app.py` (from the `# --- Version check` comment through the end of `version_check`) with:

```python
# --- Version check (yapp GitHub releases; shared with the TUI and `yapp update`) ---

@app.get("/api/version_check")
async def version_check():
    """Report whether a newer yapp release exists (cached; never blocks on errors)."""
    import updates
    data_dir = config.get("server", {}).get("data_dir", "./data")
    result = await asyncio.to_thread(updates.check, data_dir=data_dir, config=config)
    return JSONResponse({key: result[key] for key in ("current", "latest", "state", "url")})
```

Keep `_read_local_version` only if something else in `app.py` still calls it (search with `grep -n _read_local_version app.py`); otherwise delete it too.

In `static/chat.js` `checkForUpdate`, replace:

```js
        if (data.state === 'current' || data.state === 'unknown' || dismissed === data.latest) {
```

with

```js
        if (data.state !== 'update_available' || dismissed === data.latest) {
```

and replace:

```js
        const label = data.state === 'upstream_update' ? 'Upstream update available' : 'Update available';
        pill.href = data.url || 'https://github.com/bcurts/agentchattr/releases';
```

with

```js
        const label = 'Update available';
        pill.href = data.url || 'https://github.com/Ankitkkkk/yapp/releases';
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_version_check_api -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py static/chat.js tests/test_version_check_api.py
git commit -m "feat: point the browser update pill at yapp releases"
```

---

### Task 6: `yapp update` command

**Files:**
- Create: `cli_update.py`
- Modify: `cli.py` (`build_parser` command list and options; `main` dispatch)
- Test: `tests/test_cli_update.py`

**Interfaces:**
- Consumes: `updates.check`, `updates.apply`, `updates.install_method`, `updates.manual_instructions`, `updates.data_dir_from_config`; `cli_workspaces.WorkspaceAPI(url).server_status()` / `.restart_server(instance_id)`.
- Produces: `cli_update.run_update(args, config, *, output=print, ask=input, stdin_tty=sys.stdin.isatty, check=updates.check, apply=updates.apply, install_method=updates.install_method, api_factory=WorkspaceAPI) -> int`

- [ ] **Step 1: Write the failing tests**

```python
"""`yapp update` command."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import cli
import cli_update
from cli_api import CLIError

AVAILABLE = {'state': 'update_available', 'current': '0.5.0', 'latest': '0.6.0', 'tag': 'v0.6.0',
             'url': 'https://github.com/Ankitkkkk/yapp/releases/tag/v0.6.0',
             'archive_url': 'https://github.com/Ankitkkkk/yapp/archive/refs/tags/v0.6.0.zip', 'error': ''}
CONFIG = {'server': {'data_dir': '/tmp/yapp-test-data', 'port': 8300}}


def args(**kwargs):
    base = dict(check=False, yes=False, json=False, url=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


class UpdateCommandTests(unittest.TestCase):
    def run_update(self, arguments, *, result=AVAILABLE, method='installer', answer='y', tty=True,
                   applied=None, api=None):
        self.lines = []
        self.apply = Mock(return_value=applied or {'ok': True, 'state': 'installed',
                                                   'version': '0.6.0', 'message': 'Installed yapp 0.6.0.'})
        self.check = Mock(return_value=result)
        self.api = api or Mock()
        if api is None:
            self.api.server_status.side_effect = CLIError('Could not connect to the local yapp server')
        return cli_update.run_update(
            arguments, CONFIG, output=self.lines.append, ask=lambda prompt: answer,
            stdin_tty=lambda: tty, check=self.check, apply=self.apply,
            install_method=lambda: method, api_factory=lambda url: self.api)

    def test_check_exit_codes(self):
        self.assertEqual(self.run_update(args(check=True)), 10)
        self.assertEqual(self.run_update(args(check=True), result=dict(AVAILABLE, state='current')), 0)
        self.assertEqual(self.run_update(args(check=True), result=dict(AVAILABLE, state='unknown',
                                                                      error='Could not reach GitHub')), 1)
        self.apply.assert_not_called()
        self.check.assert_called_with(data_dir=cli_update.updates.data_dir_from_config(CONFIG),
                                      config=CONFIG, force=True, explicit=True)

    def test_check_json(self):
        self.run_update(args(check=True, json=True))
        self.assertEqual(json.loads(self.lines[-1])['state'], 'update_available')

    def test_prompt_declined_does_nothing(self):
        self.assertEqual(self.run_update(args(), answer='n'), 0)
        self.apply.assert_not_called()

    def test_non_tty_requires_yes(self):
        self.assertEqual(self.run_update(args(), tty=False), 1)
        self.assertIn('--yes', self.lines[-1])
        self.apply.assert_not_called()

    def test_yes_applies_and_restarts_ready_local_server(self):
        api = Mock()
        api.server_status.return_value = {'instance_id': 'i1', 'state': 'ready',
                                          'restart_supported': True, 'reason': ''}
        self.assertEqual(self.run_update(args(yes=True), api=api), 0)
        self.apply.assert_called_once()
        api.restart_server.assert_called_once_with('i1')
        self.assertTrue(any('Installed yapp 0.6.0.' in line for line in self.lines))

    def test_no_server_running_is_fine(self):
        self.assertEqual(self.run_update(args(yes=True)), 0)
        self.assertTrue(any('No local server' in line for line in self.lines))

    def test_unsupported_method_prints_instructions(self):
        self.assertEqual(self.run_update(args(yes=True), method='checkout'), 1)
        self.assertIn('git pull', self.lines[-1])
        self.apply.assert_not_called()

    def test_apply_failure(self):
        code = self.run_update(args(yes=True), applied={'ok': False, 'state': 'failed', 'version': '',
                                                        'message': 'Install failed: boom.'})
        self.assertEqual(code, 1)
        self.assertIn('boom', self.lines[-1])

    def test_already_current(self):
        self.assertEqual(self.run_update(args(), result=dict(AVAILABLE, state='current', current='0.6.0')), 0)
        self.assertIn('up to date', self.lines[-1])

    def test_parser_accepts_update_flags(self):
        parsed = cli.build_parser().parse_args(['update', '--check', '--json'])
        self.assertEqual((parsed.command, parsed.check, parsed.json), ('update', True, True))
        parsed = cli.build_parser().parse_args(['update', '--yes'])
        self.assertTrue(parsed.yes)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_cli_update -v`
Expected: ERROR `ModuleNotFoundError: No module named 'cli_update'`.

- [ ] **Step 3: Implement `cli_update.py`**

```python
"""`yapp update`: check for a newer release, install it, restart the local server."""

import json
import sys

import updates
from cli_api import CLIError
from cli_workspaces import WorkspaceAPI


def _restart_local_server(url, api_factory, output):
    try:
        api = api_factory(url)
        status = api.server_status()
    except (CLIError, OSError):
        output('No local server is running; the new version starts next time you run yapp.')
        return
    if not status.get('restart_supported') or status.get('state') != 'ready':
        output('Restart the yapp server to finish the update (F4 → Restart server in yapp).')
        return
    try:
        api.restart_server(status['instance_id'])
    except (CLIError, OSError) as error:
        output(f'Could not restart the server ({error}); restart it from yapp with F4 → Restart server.')
        return
    output('Server restarted. Open yapp windows reopen on the new version automatically.')


def run_update(args, config, *, output=print, ask=input, stdin_tty=sys.stdin.isatty,
               check=updates.check, apply=updates.apply, install_method=updates.install_method,
               api_factory=WorkspaceAPI):
    data_dir = updates.data_dir_from_config(config)
    result = check(data_dir=data_dir, config=config, force=True, explicit=True)
    if args.check:
        if args.json:
            output(json.dumps(result))
        elif result['state'] == 'update_available':
            output(f"yapp {result['latest']} is available (you have {result['current']}): {result['url']}")
        elif result['state'] == 'current':
            output(f"yapp {result['current']} is up to date.")
        else:
            output(f"Could not check for updates: {result['error']}")
        return {'update_available': 10, 'current': 0}.get(result['state'], 1)
    if result['state'] == 'current':
        output(f"yapp {result['current']} is up to date.")
        return 0
    if result['state'] != 'update_available':
        output(f"Could not check for updates: {result['error']}")
        return 1
    method = install_method()
    if method not in ('installer', 'pipx'):
        output(updates.manual_instructions(method))
        return 1
    output(f"yapp {result['latest']} is available (you have {result['current']}).\n"
           f"Release notes: {result['url']}")
    if not args.yes:
        if not stdin_tty():
            output('Run yapp update --yes to update without a terminal.')
            return 1
        if ask('Update now? [y/N] ').strip().lower() not in ('y', 'yes'):
            return 0
    output(f"Installing yapp {result['latest']}…")
    outcome = apply(result, method=method, data_dir=data_dir)
    if args.json:
        output(json.dumps(outcome))
    if not outcome['ok']:
        output(outcome['message'])
        return 1
    output(outcome['message'])
    url = args.url or f"http://127.0.0.1:{config.get('server', {}).get('port', 8300)}"
    _restart_local_server(url, api_factory, output)
    return 0
```

In `cli.py` `build_parser`: add `update=False`-style defaults by extending the existing `parser.set_defaults(...)` call with `check=False`; add `("update", "Check for and install yapp updates")` to `command_help` (after `("archive", ...)`); and in the per-command `if/elif` chain add:

```python
        elif command == "update":
            subparser.add_argument("--check", action="store_true", default=argparse.SUPPRESS,
                                   help="Only report whether an update is available")
            subparser.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                                   help="Install without asking")
```

In `cli.py` `main`, directly after `args.command = args.command or "chat"` and the `--plain` check, add:

```python
    if args.command == "update":
        from cli_update import run_update
        with redirect_stdout(sys.stderr):
            config = load_config()
        code = run_update(args, config)
        if code:
            parser.exit(code)
        return
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_cli_update tests.test_cli -v`
Expected: all PASS (including the README example test in `tests.test_cli`).

- [ ] **Step 5: Commit**

```bash
git add cli_update.py cli.py tests/test_cli_update.py
git commit -m "feat: add yapp update command"
```

---

### Task 7: Automatic updates inside the TUI

**Files:**
- Create: `cli_tui_update.py`
- Modify: `cli_workspace_chat.py` (`WorkspaceChatController.busy`)
- Modify: `cli_tui.py` (`TuiApplication.__init__`, new host methods, `run`, `interactive_tui`)
- Modify: `cli_tui_view.py` (`action_choices`)
- Modify: `cli_tui_dialogs.py` (`TuiWorkflows._run`)
- Modify: `cli.py` (chat branch of `main`)
- Test: `tests/test_cli_tui_update.py`

**Interfaces:**
- Consumes: `updates.check`, `updates.apply`, `updates.install_method`, `updates.installed_version`, `updates.RUNNING_VERSION`, `updates.auto_enabled`, `updates.manual_instructions`, `updates.save_relaunch_state`, `updates.load_relaunch_state`, `DraftStore.entries()`.
- Produces:
  - `cli_tui_update.AutoUpdater(host, *, data_dir, config, check=..., apply=..., install_method=..., installed_version=..., running_version=..., sleep=asyncio.sleep, check_interval=updates.CHECK_TTL, poll_interval=60, idle_interval=2)` with `async run()` and `async update_now() -> ActionOutcome`.
  - Host protocol implemented by `TuiApplication`: `notice(text)`, `update_safe() -> bool`, `async confirm(text, *, default=False, escape=False) -> bool`, `async restart_server_for_update() -> str | None`, `async relaunch_for_update(version: str, restart_problem: str | None) -> None`.
  - `TuiApplication(..., updater_factory=None, restored=None)`; attribute `relaunch_requested: bool`.
  - `interactive_tui(client, controller, *, initial_notices=(), restored=None, updater_factory=None) -> bool` (True means relaunch).
  - `WorkspaceChatController.busy -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_tui_update.py`:

```python
"""Automatic update orchestration inside the TUI, with a fake host."""
import asyncio
import unittest
from unittest.mock import Mock

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
            installed=lambda: '0.5.0', clock=None):
    check = Mock(return_value=result)
    apply = Mock(return_value=applied)
    auto = AutoUpdater(host, data_dir='/tmp/d', config=config or {}, check=check, apply=apply,
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
```

Also append these application-level tests to `tests/test_cli_tui_application.py` inside `ApplicationTests` (they use the existing `self.ui(...)` helper and `workspace()`):

```python
    async def test_update_host_saves_drafts_and_requests_relaunch(self):
        import tempfile
        import updates
        with tempfile.TemporaryDirectory() as data:
            async with self.ui(selected=workspace()) as ui:
                await self.connected(ui)
                ui.controller.data_dir = data
                await ui.type_text('keep me')
                self.assertTrue(ui.tui.update_safe())
                await ui.tui.relaunch_for_update('0.6.0', 'restart failed')
                await ui.wait_until(lambda: ui.task.done())
                self.assertTrue(ui.tui.relaunch_requested)
            state = updates.load_relaunch_state(data)
            self.assertEqual(state['session_id'], workspace()['id'])
            self.assertEqual([text for _, text, _ in state['drafts']], ['keep me'])
            self.assertEqual(state['notices'][0], 'Updated to yapp 0.6.0.')
            self.assertIn('restart failed', state['notices'][1])

    async def test_update_not_safe_while_dialog_open(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            task = ui.start(ui.tui.confirm('Question? [y/N]'))
            await ui.wait_until(lambda: 'Question?' in ui.screen_text())
            self.assertFalse(ui.tui.update_safe())
            await ui.key('Enter')
            await task
            await ui.wait_until(lambda: ui.tui.update_safe())

    async def test_restored_drafts_and_notices_appear(self):
        restored = {'session_id': workspace()['id'], 'channel': None,
                    'drafts': [(('session', workspace()['id']), 'restored text', 3)],
                    'notices': ['Updated to yapp 0.6.0.']}
        async with self.ui(selected=workspace(), restored=restored) as ui:
            await self.connected(ui)
            await ui.wait_until(lambda: ui.view.composer.text == 'restored text')
            self.assertIn('Updated to yapp 0.6.0.', ui.state.notices.lines)
```

If `ui.start` is not available on the application harness, use `asyncio.create_task(...)` instead (the workflow harness exposes `start`; check `tests/_tui_harness.py`). If `self.ui(...)` does not forward unknown keyword arguments to `application_harness`, extend its signature to accept `restored=None` and pass it through as `restored=restored`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_cli_tui_update -v`
Expected: ERROR `ModuleNotFoundError: No module named 'cli_tui_update'`.

- [ ] **Step 3: Implement `cli_tui_update.py`**

```python
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
                self.host.notice(f'Update check failed ({type(error).__name__}); will retry later.')
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
        if self._busy:
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
                self.host.notice(f"Automatic update to {result['latest']} failed: {outcome['message']} "
                                 'F4 → Update yapp to retry.')
                return ActionOutcome('failed', outcome['message'])
            problem = await self.host.restart_server_for_update()
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
```

Note on `test_external_update_triggers_relaunch` and `test_locked_update_waits_then_relaunches_on_version_change`: `_relaunch_if_installed_elsewhere` runs after every check and every poll tick, so a `VERSION` change on disk (another window or `yapp update`) triggers a relaunch without a second install or server restart.

- [ ] **Step 4: Run AutoUpdater tests**

Run: `.venv/bin/python -m unittest tests.test_cli_tui_update -v`
Expected: all PASS.

- [ ] **Step 5: Wire the TUI host**

In `cli_workspace_chat.py`, add to `WorkspaceChatController` (next to `wait_pending`):

```python
    @property
    def busy(self):
        """True while a user action or server mutation is in flight."""
        return bool(self._pending_actions or self._pending_mutations)
```

In `cli_tui.py`:

1. Add imports: `import updates` near the other local imports.
2. Change the `TuiApplication.__init__` signature to add `updater_factory=None, restored=None`, and at the end of `__init__` add:

```python
        self.relaunch_requested = False
        self.updater = None
        self._updater_factory = updater_factory
        self.callbacks['update'] = self.update_now
        if restored:
            for key, text, cursor in restored['drafts']:
                self.state.drafts.set(key, text, cursor=cursor)
            for text in restored['notices']:
                self.state.notices.add(text)
```

(The `callbacks` dict is created earlier in `__init__`; `TuiView` holds the same dict object, so adding the key afterwards is visible to the view.)

3. Add host methods to `TuiApplication`:

```python
    _BUSY_ROLES = frozenset(('action', 'navigation', 'dialog', 'startup', 'handoff',
                             'foreground', 'quit', 'quit_waiter'))

    def update_safe(self):
        """True only on the main screen: no dialog, help, attach, or pending work."""
        return (self._admitted() and self.dialogs.future is None and not self.view.help_visible
                and (self.handoff_task is None or self.handoff_task.done())
                and not (self._BUSY_ROLES & set(self._tasks.values()))
                and not self.controller.busy)

    async def restart_server_for_update(self):
        api = self.controller.api
        try:
            status = await asyncio.to_thread(api.server_status)
            if not status.get('restart_supported'):
                return status.get('reason') or 'This server cannot restart itself.'
            if status.get('state') != 'ready':
                return 'The server was busy ' + str(status.get('state')) + '.'
            await asyncio.to_thread(api.restart_server, status['instance_id'])
        except CLIError as error:
            return str(error)
        except (OSError, TimeoutError, KeyError):
            return 'Could not restart the server.'
        return None

    async def relaunch_for_update(self, version, restart_problem=None):
        notices = [f'Updated to yapp {version}.']
        if restart_problem:
            notices.append('Restart the server to finish the update (F4 → Restart server): '
                           + restart_problem)
        workspace = self.controller.workspace
        plain = self.controller.plain_channel
        try:
            updates.save_relaunch_state(
                self.controller.data_dir, drafts=list(self.state.drafts.entries()),
                session_id=None if plain or workspace is None else workspace['id'],
                channel=self.client.channel if plain else None, notices=notices)
        except OSError:
            self.notice('Could not save drafts; update installed, restart yapp to use it.')
            return
        self.relaunch_requested = True
        await self.request_quit(signal=True)

    async def update_now(self):
        if self.updater is None:
            self.notice('Updates are not available in this window.')
            return ActionOutcome('failed')
        return await self.updater.update_now()
```

4. In `TuiApplication.run`, replace the `pre_run=lambda: self._spawn(self._startup(), 'startup')` argument with `pre_run=self._pre_run`, and add:

```python
    def _pre_run(self):
        self._spawn(self._startup(), 'startup')
        if self._updater_factory is not None and self.controller.data_dir:
            self.updater = self._updater_factory(self)
            self._spawn(self.updater.run(), 'update')
```

and in `run`'s outer `finally:` block, before `self._close_view()`, cancel it:

```python
            for task, role in list(self._tasks.items()):
                if role == 'update' and not task.done():
                    task.cancel()
```

5. Replace `interactive_tui` with:

```python
async def interactive_tui(client, controller, *, initial_notices=(), restored=None,
                          updater_factory=None):
    """Run the TUI; return True when it exited to relaunch after an update."""
    app = TuiApplication(client, controller, initial_notices=initial_notices,
                         restored=restored, updater_factory=updater_factory)
    await app.run()
    return app.relaunch_requested
```

In `cli_tui_view.py` `action_choices`, add after the `('restart_server', ...)` tuple:

```python
                   ('update', 'Update yapp', 'Install the latest release and reopen yapp; keeps agents and drafts'),
```

In `cli_tui_dialogs.py` `TuiWorkflows._run`, add as the first branch:

```python
        if action == 'update':
            callback = self.view.callbacks.get('update')
            if callback is None:
                return ActionOutcome('cancelled')
            return await callback()
```

In `cli.py` chat branch of `main`:

- After `startup_status = ensure_server(...)`, add:

```python
            import updates
            restored = None
            if startup_status.get('data_dir'):
                restored = updates.load_relaunch_state(startup_status['data_dir'])
            selector = args.session
            if restored and selector is None and args.channel is None:
                selector = restored['session_id']
            if restored and args.channel is not None and restored['channel']:
                client.channel = restored['channel']
```

- Pass `selector=selector` (instead of `selector=args.session`) to `WorkspaceChatController(...)`.
- Replace the `asyncio.run(interactive_tui(client, controller, initial_notices=initial_notices))` call with:

```python
                    from cli_tui_update import AutoUpdater
                    relaunch = asyncio.run(interactive_tui(
                        client, controller, initial_notices=initial_notices, restored=restored,
                        updater_factory=lambda host: AutoUpdater(
                            host, data_dir=controller.data_dir, config=config)))
```

- After the whole `try/except` that runs the TUI (still inside `if args.command == "chat":`, after the `else:` TUI branch completes), add:

```python
                if relaunch:
                    os.execv(sys.executable, [sys.executable, *sys.argv])
```

(Initialize `relaunch = False` before `if mode == "plain":` so the plain branch leaves it False.)

- [ ] **Step 6: Run all TUI and CLI tests**

Run: `.venv/bin/python -m unittest tests.test_cli_tui_update tests.test_cli_tui_application tests.test_cli_tui_workflows tests.test_cli_tui_restart tests.test_cli_tui_view tests.test_cli -v 2>&1 | tail -30`
Expected: all PASS. If a test asserts the exact list of F4 choices, update its expected list to include `('update', 'Update yapp', ...)` right after `restart_server`.

- [ ] **Step 7: Commit**

```bash
git add cli_tui_update.py cli_tui.py cli_tui_view.py cli_tui_dialogs.py cli_workspace_chat.py cli.py tests/test_cli_tui_update.py tests/test_cli_tui_application.py
git commit -m "feat: install yapp updates automatically from the TUI"
```

---

### Task 8: Documentation, config example, and end-to-end check

**Files:**
- Modify: `README.md`, `INSTALLATION.md`, `config.toml`

- [ ] **Step 1: Document**

Append to `config.toml`:

```toml

# Updates: yapp checks GitHub Releases every 6 hours while the TUI is open and
# installs new releases automatically (agents keep running; drafts are kept).
# [updates]
# auto = false    # only notify; update with F4 → Update yapp or `yapp update`
# check = false   # never check in the background
```

In `README.md`, after the Quick start paragraph ending "Run it again to update.", add:

```markdown
### Staying up to date

yapp updates itself. While the TUI is open it checks for a new
[release](https://github.com/Ankitkkkk/yapp/releases) every 6 hours; when one
appears it installs it, restarts the server, and reopens, keeping your agents
running and your unsent drafts. Update by hand with `yapp update` (or
F4 → Update yapp); `yapp update --check` only reports. Turn automatic installs
off with `[updates] auto = false` in `config.local.toml` (or
`YAPP_NO_AUTO_UPDATE=1`), or stop checking with `[updates] check = false`
(`YAPP_NO_UPDATE_CHECK=1`). Source checkouts are never updated automatically;
use `git pull`.
```

In `README.md`, in the "Project and upstream" section, add:

```markdown
### Publishing a release

1. Bump `VERSION` (for example to `0.6.0`) and merge to `main`.
2. Create a GitHub Release tagged `v0.6.0` with release notes.

Installed copies pick it up within 6 hours. The tag must match `VERSION` on
that commit, or yapp reports the wrong version after updating.
```

In `INSTALLATION.md` section 2, replace "Run it again at any time to update." with "yapp then keeps itself up to date (see the README's *Staying up to date*); running the installer again also updates it."

- [ ] **Step 2: End-to-end check with a fake release**

Run from the repository root (uses scratch dirs; nothing touches the real install):

```sh
S=$(mktemp -d)
HOME=$S/home YAPP_SOURCE=$PWD PATH=/usr/bin:/bin sh install.sh
# Build a "newer" archive: copy the repo, bump VERSION, zip it.
git archive --format=zip --prefix=yapp-0.9.0/ HEAD -o $S/base.zip
python3 - "$S" <<'PY'
import sys, zipfile
s = sys.argv[1]
with zipfile.ZipFile(f'{s}/base.zip') as src, zipfile.ZipFile(f'{s}/yapp-0.9.0.zip', 'w') as dst:
    for item in src.infolist():
        data = b'0.9.0\n' if item.filename == 'yapp-0.9.0/VERSION' else src.read(item)
        dst.writestr(item, data)
PY
echo '{"tag_name": "v0.9.0", "html_url": "https://example.invalid/v0.9.0"}' > $S/release.json
(cd $S && python3 -m http.server 18431 >/dev/null 2>&1 &)
HOME=$S/home YAPP_UPDATE_URL=http://127.0.0.1:18431/release.json YAPP_UPDATE_ARCHIVE=$S/yapp-0.9.0.zip \
  $S/home/.local/bin/yapp update --check; echo "exit=$?"
HOME=$S/home YAPP_UPDATE_URL=http://127.0.0.1:18431/release.json YAPP_UPDATE_ARCHIVE=$S/yapp-0.9.0.zip \
  $S/home/.local/bin/yapp update --yes; echo "exit=$?"
$S/home/.local/share/yapp/venv/bin/python -c "from importlib.metadata import version; print(version('yapp'))"
rm -rf build yapp.egg-info
```

Expected: `--check` prints `yapp 0.9.0 is available ...` and `exit=10`; `--yes` prints `Installed yapp 0.9.0.` and `No local server is running...`, `exit=0`; the last command prints `0.9.0`. Stop the `http.server` afterwards (find its PID with `ss -ltnp | grep 18431` and `kill` it; do not use `pkill -f`, which can match your own shell).

- [ ] **Step 3: Full test suite**

Run: `timeout 900 .venv/bin/python -m unittest discover -s tests 2>&1 | grep -E '^Ran |^OK|^FAILED'`
Expected: `OK` (with the existing 2 skips).

- [ ] **Step 4: Commit**

```bash
git add README.md INSTALLATION.md config.toml
git commit -m "docs: explain automatic updates and the release routine"
```
