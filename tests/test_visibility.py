"""Visibility policy on every agent-facing read path (spec §1, §3)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_bridge
from store import MessageStore
from summaries import SummaryStore
from workspace_unread import BLOCKED_TEXT


class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MessageStore(str(Path(self.tmp.name) / "messages.jsonl"))
        self.summaries = SummaryStore(str(Path(self.tmp.name) / "summaries.json"))
        self.policies = {}     # (sender, channel) -> policy dict | None
        self.acks = []         # (sender, channel, ids)
        self._saved = (mcp_bridge.store, mcp_bridge.summaries, mcp_bridge.registry,
                       mcp_bridge.workspace_policy, mcp_bridge.workspace_ack,
                       dict(mcp_bridge._cursors), dict(mcp_bridge._last_read_channel),
                       dict(mcp_bridge._empty_read_count))
        mcp_bridge.store = self.store
        mcp_bridge.summaries = self.summaries
        mcp_bridge.registry = None
        mcp_bridge.workspace_policy = lambda s, c: self.policies.get((s, c))
        mcp_bridge.workspace_ack = lambda s, c, ids: self.acks.append((s, c, list(ids)))
        mcp_bridge._cursors.clear()
        mcp_bridge._last_read_channel.clear()
        mcp_bridge._empty_read_count.clear()
        for i in range(6):                       # ids 0..5 in ws-x
            self.store.add("ankit", f"m{i}", channel="ws-x")
        self.store.add("system", "private", msg_type="summary", channel="ws-x",
                       metadata={"audience": ["ag_1"]})   # id 6

    def tearDown(self):
        (mcp_bridge.store, mcp_bridge.summaries, mcp_bridge.registry,
         mcp_bridge.workspace_policy, mcp_bridge.workspace_ack, cursors, lrc, erc) = self._saved
        mcp_bridge._cursors.clear(); mcp_bridge._cursors.update(cursors)
        mcp_bridge._last_read_channel.clear(); mcp_bridge._last_read_channel.update(lrc)
        mcp_bridge._empty_read_count.clear(); mcp_bridge._empty_read_count.update(erc)

    def ids(self, out):
        return [m["id"] for m in json.loads(out.split("\nhas_more")[0])]

    def test_non_workspace_agent_is_unaffected(self):
        out = mcp_bridge.chat_read(sender="gemini", channel="ws-x", limit=50)
        self.assertEqual(self.ids(out), [0, 1, 2, 3, 4, 5])   # no floor, but the private summary (6) is hidden
        self.assertEqual(self.acks, [])

    def test_floor_applies_to_first_read_cursor_read_and_since_id(self):
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": 3}
        out = mcp_bridge.chat_read(sender="claude-1", channel="ws-x", limit=50)
        self.assertEqual(self.ids(out), [3, 4, 5, 6])
        self.store.add("ankit", "m7", channel="ws-x")
        out = mcp_bridge.chat_read(sender="claude-1", channel="ws-x", limit=50)   # cursor read
        self.assertEqual(self.ids(out), [7])
        out = mcp_bridge.chat_read(sender="claude-1", channel="ws-x", since_id=-1, limit=50)
        self.assertEqual(self.ids(out), [3, 4, 5, 6, 7])

    def test_explicit_since_id_pages_oldest_first_with_has_more(self):
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": 0}
        out = mcp_bridge.chat_read(sender="claude-1", channel="ws-x", since_id=-1, limit=3)
        self.assertEqual(self.ids(out), [0, 1, 2])
        self.assertIn("has_more: true, next_since_id: 2", out)
        out = mcp_bridge.chat_read(sender="claude-1", channel="ws-x", since_id=2, limit=3)
        self.assertEqual(self.ids(out), [3, 4, 5])
        out = mcp_bridge.chat_read(sender="claude-1", channel="ws-x", since_id=0, limit=50)
        self.assertEqual(self.ids(out), [1, 2, 3, 4, 5, 6])
        self.assertNotIn("has_more", out)

    def test_message_zero_visible_under_literal(self):
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": 0}
        out = mcp_bridge.chat_read(sender="claude-1", channel="ws-x", since_id=-1, limit=50)
        self.assertEqual(self.ids(out)[0], 0)

    def test_audience_hides_private_summary_from_others(self):
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": 0}
        self.policies[("codex-1", "ws-x")] = {"agent_id": "ag_2", "floor_id": 0}
        self.assertIn(6, self.ids(mcp_bridge.chat_read(sender="claude-1", channel="ws-x", since_id=-1, limit=50)))
        self.assertNotIn(6, self.ids(mcp_bridge.chat_read(sender="codex-1", channel="ws-x", since_id=-1, limit=50)))

    def test_blocked_when_floor_missing(self):
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": None}
        self.assertEqual(mcp_bridge.chat_read(sender="claude-1", channel="ws-x"), BLOCKED_TEXT)
        self.assertEqual(mcp_bridge.chat_resync(sender="claude-1", channel="ws-x"), BLOCKED_TEXT)
        self.assertEqual(self.acks, [])

    def test_all_channels_read_applies_each_channel_floor(self):
        self.store.add("ankit", "g0", channel="general")
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": 5}
        out = mcp_bridge.chat_read(sender="claude-1", since_id=-1, limit=50)
        got = self.ids(out)
        self.assertNotIn(2, got)
        self.assertIn(5, got)
        self.assertIn(7, got)   # general message, no policy there

    def test_reads_ack_only_what_they_return(self):
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": 0}
        mcp_bridge.chat_read(sender="claude-1", channel="ws-x", since_id=2, limit=2)
        self.assertEqual(self.acks, [("claude-1", "ws-x", [3, 4])])
        mcp_bridge.chat_resync(sender="claude-1", channel="ws-x", limit=2)
        self.assertEqual(self.acks[-1], ("claude-1", "ws-x", [5, 6]))

    def test_chat_send_does_not_ack(self):
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": 0}
        mcp_bridge.chat_send(sender="claude-1", message="hi", channel="ws-x")
        self.assertEqual(self.acks, [])

    def test_chat_summary_read_is_gated(self):
        self.summaries.write("ws-x", "old summary", "codex-1", message_id=2)
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": 3}
        out = json.loads(mcp_bridge.chat_summary(action="read", sender="claude-1", channel="ws-x"))
        self.assertIsNone(out["text"])
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": 0}
        out = json.loads(mcp_bridge.chat_summary(action="read", sender="claude-1", channel="ws-x"))
        self.assertEqual(out["text"], "old summary")
        self.policies[("claude-1", "ws-x")] = {"agent_id": "ag_1", "floor_id": None}
        self.assertEqual(mcp_bridge.chat_summary(action="read", sender="claude-1", channel="ws-x"), BLOCKED_TEXT)


if __name__ == "__main__":
    unittest.main()
