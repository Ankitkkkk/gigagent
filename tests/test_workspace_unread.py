"""Pure unread/visibility functions (spec §1, §4)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workspace_unread import BLOCKED_TEXT, apply_acks, bundle_prompt, is_blocked, unread, visible


def msg(i, sender="ankit", audience=None, mtype="chat", channel="ws-x"):
    m = {"id": i, "sender": sender, "text": f"m{i}", "type": mtype, "channel": channel, "time": ""}
    if audience is not None:
        m["metadata"] = {"audience": audience}
    return m


def agent(**kw):
    a = {"agent_id": "ag_1", "registry_name": "claude-1", "floor_id": 0,
         "read_mark": -1, "acked_above_mark": []}
    a.update(kw)
    return a


class VisibleTests(unittest.TestCase):
    def test_floor_is_inclusive_and_zero_shows_message_zero(self):
        pol = {"agent_id": "ag_1", "floor_id": 0}
        self.assertTrue(visible(pol, msg(0)))
        pol = {"agent_id": "ag_1", "floor_id": 101}
        self.assertFalse(visible(pol, msg(100)))
        self.assertTrue(visible(pol, msg(101)))

    def test_audience(self):
        pol = {"agent_id": "ag_1", "floor_id": 0}
        self.assertTrue(visible(pol, msg(5, audience=["ag_1"])))
        self.assertFalse(visible(pol, msg(5, audience=["ag_2"])))
        self.assertFalse(visible(None, msg(5, audience=["ag_1"])))   # non-member never sees private
        self.assertTrue(visible(None, msg(5)))                        # non-member, no floor

    def test_blocked_when_floor_missing(self):
        self.assertTrue(is_blocked({"agent_id": "ag_1", "floor_id": None}))
        self.assertFalse(is_blocked({"agent_id": "ag_1", "floor_id": 0}))
        self.assertIn("/history", BLOCKED_TEXT)


class UnreadTests(unittest.TestCase):
    def test_definition(self):
        a = agent(read_mark=140, acked_above_mark=[143])
        msgs = [msg(i) for i in range(139, 153)]
        msgs[150 - 139]["sender"] = "claude-1"          # own message
        msgs[145 - 139]["type"] = "system"              # wrong type
        routing = {141: ["ag_1"], 143: ["ag_1"], 145: ["ag_1"], 147: ["ag_1"],
                   150: ["ag_1"], 152: ["ag_1"], 139: ["ag_1"], 151: ["ag_2"]}
        self.assertEqual([m["id"] for m in unread(a, msgs, routing)], [141, 147, 152])

    def test_send_then_read_does_not_clear_gap(self):
        a = agent(read_mark=140)
        routing = {141: ["ag_1"], 149: ["ag_1"], 151: ["ag_1"]}
        mark, acked = apply_acks(a["read_mark"], a["acked_above_mark"], [151], [141, 149, 151])
        self.assertEqual((mark, acked), (140, [151]))
        msgs = [msg(141), msg(149), msg(151)]
        self.assertEqual([m["id"] for m in unread(agent(read_mark=mark, acked_above_mark=acked), msgs, routing)],
                         [141, 149])

    def test_apply_acks_ignores_unrouted_ids(self):
        self.assertEqual(apply_acks(-1, [], [5, 6], [6]), (6, []))


class BundleTests(unittest.TestCase):
    def test_single_message_is_plain_mention_prompt(self):
        p = bundle_prompt("ws-x", [msg(143)])
        self.assertIn("since_id=142", p)
        self.assertIn("#143", p)
        self.assertNotIn("End of missed messages", p)

    def test_many_messages_have_marker(self):
        p = bundle_prompt("ws-x", [msg(141), msg(147, sender="codex-1"), msg(152)])
        self.assertIn("3 messages", p)
        self.assertIn("since_id=140", p)
        self.assertIn("#141 (ankit)", p)
        self.assertIn("#147 (codex-1)", p)
        self.assertTrue(p.strip().endswith("nothing else is pending for you."))
        self.assertIn("End of missed messages: 3 in total", p)

    def test_over_fifty_is_truncated(self):
        p = bundle_prompt("ws-x", [msg(i) for i in range(100, 160)])
        self.assertIn("60 messages", p)
        self.assertIn("… and 50 more", p)
        self.assertIn("#100 (ankit)", p)
        self.assertIn("#159 (ankit)", p)
        self.assertNotIn("#130", p)


if __name__ == "__main__":
    unittest.main()
