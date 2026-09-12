"""Recipients recorded from _handle_new_message (spec §4)."""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

import app as app_module
from _workspace_helpers import app_cfg as cfg


class RoutingRecipientsTests(unittest.TestCase):
    ROUTING_DEFAULT = "none"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        app_module.configure(cfg(self.tmp, default=self.ROUTING_DEFAULT))
        self.ws_store = app_module.workspace_store
        self.ws = self.ws_store.create("proj")
        launch = {"kind": "spawn", "nonce": "n", "at": "t", "pid": None}
        self.stopped = self.ws_store.add_agent(self.ws["id"], provider="claude", cwd="/p",
            history_mode="literal", registry_name="claude-1", floor_id=0,
            native_session_id=None, history_state="done", last_launch=launch)
        self.ws_store.update_agent(self.ws["id"], self.stopped["agent_id"], last_state="exited")
        self.running = self.ws_store.add_agent(self.ws["id"], provider="codex", cwd="/p",
            history_mode="literal", registry_name="codex-1", floor_id=0,
            native_session_id=None, history_state="done", last_launch=launch)
        self.ws_store.update_agent(self.ws["id"], self.running["agent_id"], last_state="running")
        app_module.registry.register("codex", preferred_name="codex-1")

    def post(self, text, sender="ankit"):
        msg = app_module.store.add(sender, text, channel=self.ws["channel"])
        asyncio.run(app_module._handle_new_message(msg))
        return msg["id"]

    def routed(self, mid):
        return self.ws_store.routing_for(self.ws["id"]).get(mid, [])

    def test_router_vocabulary_includes_stopped_member_and_family(self):
        self.assertIn("claude-1", app_module.router.agent_names)
        self.assertIn("claude", app_module.router.agent_names)
        self.assertEqual(app_module.router.mention_tokens("hi @claude-1 and @ALL"), ["claude-1", "all"])

    def test_explicit_mention_of_stopped_agent_is_recorded(self):
        mid = self.post("@claude-1 please look")
        self.assertEqual(self.routed(mid), [self.stopped["agent_id"]])

    def test_family_mention_reaches_stopped_member(self):
        mid = self.post("@claude thoughts?")
        self.assertEqual(self.routed(mid), [self.stopped["agent_id"]])

    def test_at_all_reaches_running_only(self):
        mid = self.post("@all status?")
        self.assertEqual(self.routed(mid), [self.running["agent_id"]])

    def test_no_mention_under_default_none_has_no_recipients(self):
        mid = self.post("just chatting")
        self.assertEqual(self.routed(mid), [])

    def test_renamed_recipient_still_matches_by_agent_id(self):
        mid = self.post("@claude-1 hi")
        self.ws_store.rename_agent("claude-1", "reviewer")
        self.assertEqual(self.routed(mid), [self.stopped["agent_id"]])
        self.assertIn("reviewer", app_module.router.agent_names)

    def test_replay_recovers_messages_persisted_before_a_crash(self):
        routed_mid = self.post("@claude-1 before crash")
        # Simulate the crash window: persisted, observer never ran
        lost = app_module.store.add("ankit", "@claude-1 lost one", channel=self.ws["channel"])
        plain = app_module.store.add("ankit", "no mention", channel=self.ws["channel"])
        self.assertEqual(self.routed(lost["id"]), [])
        app_module._replay_unrouted()
        self.assertEqual(self.routed(lost["id"]), [self.stopped["agent_id"]])
        self.assertEqual(self.routed(plain["id"]), [])
        self.assertEqual(self.ws_store.routing_high_water(self.ws["id"]), plain["id"])
        self.assertEqual(self.routed(routed_mid), [self.stopped["agent_id"]])

    def test_skipped_message_types_do_not_pin_the_watermark(self):
        sysmsg = app_module.store.add("system", "claude-1 appears offline", msg_type="system",
                                      channel=self.ws["channel"])
        asyncio.run(app_module._handle_new_message(sysmsg))       # returns before routing
        chat = self.post("@claude-1 after the system line")
        app_module._compact_routing_marks()
        self.assertEqual(self.ws_store.routing_high_water(self.ws["id"]), chat["id"] if isinstance(chat, dict) else chat)
        self.assertEqual(self.ws_store.get(self.ws["id"]).get("routing_done", []), [])
        self.assertEqual(self.routed(sysmsg["id"]), [])

    def test_loop_guard_return_still_marks_the_message(self):
        app_module.router._get_ch(self.ws["channel"])["paused"] = True
        mid = self.post("@codex-1 while paused", sender="codex-1")
        app_module._compact_routing_marks()
        self.assertGreaterEqual(self.ws_store.routing_high_water(self.ws["id"]), mid)
        app_module.router._get_ch(self.ws["channel"])["paused"] = False

    def test_replay_fills_a_gap_left_by_out_of_order_observers(self):
        first = app_module.store.add("ankit", "@claude-1 first", channel=self.ws["channel"])
        second = app_module.store.add("ankit", "@claude-1 second", channel=self.ws["channel"])
        asyncio.run(app_module._handle_new_message(second))      # observer for the later message finished
        app_module._compact_routing_marks()
        self.assertEqual(self.ws_store.routing_high_water(self.ws["id"]), -1)   # gap: first not done
        self.assertEqual(self.routed(first["id"]), [])
        app_module._replay_unrouted()                             # "server restart"
        self.assertEqual(self.routed(first["id"]), [self.stopped["agent_id"]])
        self.assertEqual(self.routed(second["id"]), [self.stopped["agent_id"]])
        latest = app_module.store.get_recent(1, channel=self.ws["channel"])[-1]
        self.assertEqual(self.ws_store.routing_high_water(self.ws["id"]), latest["id"])
        self.assertEqual(latest["type"], "system")
        self.assertEqual(self.routed(latest["id"]), [])


class RoutingDefaultAllTests(RoutingRecipientsTests):
    """Same fixture with routing.default = "all": a no-mention message is a broadcast."""
    ROUTING_DEFAULT = "all"

    def test_no_mention_under_default_none_has_no_recipients(self):
        self.skipTest("default is 'all' in this fixture")

    def test_no_mention_under_default_all_reaches_running_only(self):
        mid = self.post("just chatting")
        self.assertEqual(self.routed(mid), [self.running["agent_id"]])


if __name__ == "__main__":
    unittest.main()
