"""Release checks, install methods, apply, and relaunch state."""
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace

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


if __name__ == '__main__':
    unittest.main()
