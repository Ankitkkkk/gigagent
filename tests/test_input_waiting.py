"""Transient terminal prompt hints never become durable lifecycle state."""
import unittest
from unittest.mock import patch

import mcp_bridge


class InputWaitingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(mcp_bridge._input_waiting, {}, clear=True))

    def test_refresh_clear_and_expiry(self):
        with patch('mcp_bridge.time.time', return_value=100):
            mcp_bridge.set_waiting_for_input('hint-test', True)
        with patch('mcp_bridge.time.time', return_value=109):
            self.assertTrue(mcp_bridge.is_waiting_for_input('hint-test'))
            mcp_bridge.set_waiting_for_input('hint-test', True)
        with patch('mcp_bridge.time.time', return_value=121):
            self.assertTrue(mcp_bridge.is_waiting_for_input('hint-test'))
        with patch('mcp_bridge.time.time', return_value=130):
            self.assertFalse(mcp_bridge.is_waiting_for_input('hint-test'))
        mcp_bridge.set_waiting_for_input('hint-test', True)
        mcp_bridge.set_waiting_for_input('hint-test', False)
        self.assertFalse(mcp_bridge.is_waiting_for_input('hint-test'))

    def test_rename_and_purge(self):
        with patch.object(mcp_bridge, '_save_cursors'), patch.dict(mcp_bridge._presence), \
                patch.object(mcp_bridge, '_renamed_from', set()):
            mcp_bridge.set_waiting_for_input('hint-old', True)
            mcp_bridge.migrate_identity('hint-old', 'hint-new')
            self.assertFalse(mcp_bridge.is_waiting_for_input('hint-old'))
            self.assertTrue(mcp_bridge.is_waiting_for_input('hint-new'))
            mcp_bridge.purge_identity('hint-new')
            self.assertFalse(mcp_bridge.is_waiting_for_input('hint-new'))
