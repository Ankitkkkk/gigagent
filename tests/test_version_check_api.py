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
