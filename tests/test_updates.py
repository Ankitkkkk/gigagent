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
