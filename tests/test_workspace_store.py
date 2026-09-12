"""WorkspaceStore persistence and policy (spec §1, §4, §7)."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workspace_store import WorkspaceStore


def launch(kind="spawn"):
    return {"kind": kind, "nonce": "abc", "at": "2026-09-12T10:00:00Z", "pid": None}


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "workspaces.json"
        self.store = WorkspaceStore(self.path, self.root / "identity")

    def add(self, ws, **kw):
        base = dict(provider="claude", cwd="/proj", history_mode="literal",
                    registry_name="claude-1", floor_id=0, native_session_id="sid-1",
                    history_state="pending", last_launch=launch())
        base.update(kw)
        return self.store.add_agent(ws["id"], **base)

    # --- records ---

    def test_create_defaults_name_to_id_and_channel_has_id_suffix(self):
        ws = self.store.create(None)
        self.assertTrue(ws["id"].startswith("ws_"))
        self.assertEqual(ws["name"], ws["id"])
        self.assertFalse(ws["archived"])
        ws2 = self.store.create("Billing Refactor")
        self.assertEqual(ws2["channel"], f"ws-billing-ref-{ws2['id'][3:7]}")
        self.assertLessEqual(len(ws2["channel"]), 20)          # app._CHANNEL_NAME_RE limit
        ws3 = self.store.create("Billing Refactor")
        self.assertNotEqual(ws2["channel"], ws3["channel"])

    def test_create_save_failure_restores_memory_and_allows_retry(self):
        with patch.object(self.store, "_save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.create("failed")

        self.assertEqual(self.store.list(include_archived=True), [])
        created = self.store.create("works")
        self.assertEqual([ws["id"] for ws in self.store.list(include_archived=True)], [created["id"]])

    def test_workspace_change_callbacks_identify_scoped_and_global_commits(self):
        self.assertTrue(hasattr(self.store, "on_workspace_change"))
        scoped = []
        legacy = []
        self.store.on_workspace_change(scoped.append)
        self.store.on_change(lambda: legacy.append("changed"))

        ws = self.store.create("x")
        agent = self.add(ws)
        self.store.rename(ws["id"], "renamed")
        self.store.set_archived(ws["id"], True)
        self.store.set_archived(ws["id"], False)
        self.store.update_agent(ws["id"], agent["agent_id"], last_state="running")
        self.store.update_agent_if_launch(
            ws["id"], agent["agent_id"], "abc", native_verified=True
        )
        self.store.record_routing(ws["channel"], 0, [agent["agent_id"]])
        self.store.compact_routing(ws["id"], [0])
        self.store.ack(ws["id"], agent["agent_id"], [0])
        self.store.rename_agent("claude-1", "reviewer")
        self.store.update_agent(ws["id"], agent["agent_id"], last_state="starting")
        self.store.mark_exited("reviewer")
        self.store.remove_agent(ws["id"], agent["agent_id"])

        self.assertEqual(scoped, [ws["id"]] * 10 + [None, ws["id"], None, ws["id"]])
        self.assertEqual(len(legacy), len(scoped))

    def test_list_is_newest_first_and_hides_archived(self):
        a = self.store.create("a")
        b = self.store.create("b")
        self.store.rename(a["id"], "a2")  # bumps updated_at
        self.assertEqual([w["name"] for w in self.store.list()], ["a2", "b"])
        self.store.set_archived(b["id"], True)
        self.assertEqual([w["name"] for w in self.store.list()], ["a2"])
        self.assertEqual([w["name"] for w in self.store.list(include_archived=True)], ["a2", "b"])

    def test_resolve_by_id_name_and_unique_prefix(self):
        a = self.store.create("billing")
        self.store.create("bugs")
        self.assertEqual(self.store.resolve(a["id"])["id"], a["id"])
        self.assertEqual(self.store.resolve("billing")["id"], a["id"])
        self.assertEqual(self.store.resolve("bil")["id"], a["id"])
        self.assertIsNone(self.store.resolve("b"))  # ambiguous
        self.assertIsNone(self.store.resolve("nope"))

    def test_persists_and_reloads(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        again = WorkspaceStore(self.path, self.root / "identity")
        self.assertEqual(again.get(ws["id"])["agents"][0]["agent_id"], ag["agent_id"])
        self.assertEqual(again.get_agent(ws["id"], ag["agent_id"])["read_mark"], -1)
        self.assertEqual(again.get_agent(ws["id"], ag["agent_id"])["acked_above_mark"], [])

    def test_corrupt_file_is_quarantined_with_warning(self):
        self.path.write_text("{not json")
        s = WorkspaceStore(self.path, self.root / "identity")
        self.assertEqual(s.list(), [])
        self.assertIn("corrupt", s.warning)
        self.assertTrue(list(self.root.glob("workspaces.json.corrupt-*")))

    def test_add_agent_fields_and_update(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        self.assertTrue(ag["agent_id"].startswith("ag_"))
        self.assertEqual(ag["last_state"], "starting")
        self.assertEqual(ag["previous_native_ids"], [])
        self.assertFalse(ag["native_verified"])
        self.store.update_agent(ws["id"], ag["agent_id"], last_state="running", native_verified=True)
        got = self.store.get_agent(ws["id"], ag["agent_id"])
        self.assertEqual(got["last_state"], "running")
        self.assertTrue(got["native_verified"])
        with self.assertRaises(ValueError):
            self.add(ws, history_mode="weird")

    def test_add_agent_returns_a_deep_copy(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        ag["last_launch"]["nonce"] = "mutated"
        ag["previous_cwds"].append("/other")
        got = self.store.get_agent(ws["id"], ag["agent_id"])
        self.assertEqual(got["last_launch"]["nonce"], "abc")
        self.assertEqual(got["previous_cwds"], [])

    def test_add_agent_save_failure_restores_memory_and_allows_retry(self):
        ws = self.store.create("x")
        with patch("workspace_store._now", return_value="2099-01-01T00:00:00Z"):
            with patch.object(self.store, "_save", side_effect=OSError("read only")):
                with self.assertRaisesRegex(OSError, "read only"):
                    self.add(ws)

        self.assertEqual(self.store.get(ws["id"]), ws)
        agent = self.add(ws)
        self.assertEqual(self.store.get(ws["id"])["agents"][0]["agent_id"], agent["agent_id"])

    def test_write_identity_uses_current_registry_name_not_a_stale_dict(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        self.store.write_identity(ws, ag, token="tok")
        self.store.rename_agent("claude-1", "reviewer")
        # ag is now stale: its registry_name is still "claude-1". A write built
        # from it must not revert the shadow's registry_name back.
        self.store.write_identity(ws, ag, token="tok2")
        shadow = self.store.read_identity(ag["agent_id"])
        self.assertEqual(shadow["registry_name"], "reviewer")
        self.assertEqual(shadow["token"], "tok2")

    def test_rename_agent_and_find_by_registry_name(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        self.store.write_identity(ws, ag, token="tok")
        self.assertEqual(self.store.rename_agent("claude-1", "reviewer"), 1)
        found = self.store.find_agent_by_registry_name("reviewer")
        self.assertEqual(found[1]["agent_id"], ag["agent_id"])
        self.assertIsNone(self.store.find_agent_by_registry_name("claude-1"))
        shadow = self.store.read_identity(ag["agent_id"])
        self.assertEqual(shadow["registry_name"], "reviewer")   # shadow follows the rename
        self.assertEqual(shadow["token"], "tok")

    def test_lookup_prefers_live_agent_when_names_collide(self):
        ws = self.store.create("x")
        old = self.add(ws)
        self.store.update_agent(ws["id"], old["agent_id"], last_state="exited")
        new = self.add(ws, floor_id=9)
        self.store.update_agent(ws["id"], new["agent_id"], last_state="running")
        self.assertEqual(self.store.find_agent_by_registry_name("claude-1")[1]["agent_id"], new["agent_id"])
        self.assertEqual(self.store.policy_for("claude-1", ws["channel"])["floor_id"], 9)

    def test_update_agent_if_launch_is_conditional(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        nonce = ag["last_launch"]["nonce"]
        self.assertTrue(self.store.update_agent_if_launch(ws["id"], ag["agent_id"], nonce, native_verified=True))
        self.store.update_agent(ws["id"], ag["agent_id"], last_launch=dict(ag["last_launch"], nonce="newer"))
        self.assertFalse(self.store.update_agent_if_launch(ws["id"], ag["agent_id"], nonce, native_session_id="stale"))
        self.assertEqual(self.store.get_agent(ws["id"], ag["agent_id"])["native_session_id"], "sid-1")

    def test_mark_exited(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        self.store.update_agent(ws["id"], ag["agent_id"], last_state="running")
        self.store.mark_exited("claude-1", error="boom")
        got = self.store.get_agent(ws["id"], ag["agent_id"])
        self.assertEqual(got["last_state"], "exited")
        self.assertEqual(got["last_error"], "boom")

    # --- identity shadows ---

    def test_identity_file_is_0600_and_holds_policy_shadow(self):
        ws = self.store.create("x")
        ag = self.add(ws, floor_id=42)
        p = self.store.write_identity(ws, ag, token="tok")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        data = json.loads(p.read_text())
        for key in ("registry_name", "token", "workspace_id", "agent_id", "channel",
                    "history_mode", "floor_id", "last_launch"):
            self.assertIn(key, data)
        self.assertEqual(data["floor_id"], 42)
        self.assertEqual(self.store.read_identity(ag["agent_id"])["token"], "tok")
        self.store.delete_identity(ag["agent_id"])
        self.assertIsNone(self.store.read_identity(ag["agent_id"]))

    # --- policy ---

    def test_policy_from_store_then_shadow_then_none(self):
        ws = self.store.create("x")
        ag = self.add(ws, floor_id=7)
        pol = self.store.policy_for("claude-1", ws["channel"])
        self.assertEqual(pol["agent_id"], ag["agent_id"])
        self.assertEqual(pol["floor_id"], 7)
        self.assertIsNone(self.store.policy_for("claude-1", "general"))   # other channel: no policy
        self.assertIsNone(self.store.policy_for("gemini", ws["channel"]))  # not a member
        # store lost, shadow present
        self.store.write_identity(ws, ag, token="tok")
        self.path.write_text("{broken")
        lost = WorkspaceStore(self.path, self.root / "identity")
        pol2 = lost.policy_for("claude-1", ws["channel"])
        self.assertEqual(pol2["agent_id"], ag["agent_id"])
        self.assertEqual(pol2["floor_id"], 7)

    def test_policy_floor_none_when_record_damaged(self):
        ws = self.store.create("x")
        ag = self.add(ws, floor_id=7)
        self.store.update_agent(ws["id"], ag["agent_id"], floor_id=None)
        self.assertIsNone(self.store.policy_for("claude-1", ws["channel"])["floor_id"])

    # --- routing table + acks ---

    def test_resolve_recipients_explicit_family_and_broadcast(self):
        ws = self.store.create("x")
        stopped = self.add(ws, registry_name="claude-1")
        self.store.update_agent(ws["id"], stopped["agent_id"], last_state="exited")
        running = self.add(ws, provider="codex", registry_name="codex-1", floor_id=0)
        self.store.update_agent(ws["id"], running["agent_id"], last_state="running")
        ch = ws["channel"]
        self.assertEqual(self.store.resolve_recipients(ch, ["claude-1"], []), [stopped["agent_id"]])
        self.assertEqual(self.store.resolve_recipients(ch, ["claude"], []), [stopped["agent_id"]])
        self.assertEqual(sorted(self.store.resolve_recipients(ch, ["all"], ["claude-1", "codex-1"])),
                         [running["agent_id"]])
        self.assertEqual(self.store.resolve_recipients(ch, [], []), [])
        self.assertEqual(self.store.resolve_recipients(ch, [], ["claude-1", "codex-1"]), [running["agent_id"]])
        self.assertEqual(self.store.resolve_recipients("general", ["claude-1"], []), [])

    def test_routing_watermark_only_advances_through_contiguous_ids(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        ch = ws["channel"]
        channel_ids = [3, 5, 8]                                # ids 4, 6, 7 belong to other channels
        self.store.record_routing(ch, 5, [ag["agent_id"]])    # observer for 5 finished first
        self.store.record_routing(ch, 8, [])
        self.store.compact_routing(ws["id"], channel_ids)
        self.assertEqual(self.store.routing_high_water(ws["id"]), -1)          # 3 not done → no move
        self.assertEqual(self.store.unrouted_ids(ws["id"], channel_ids), [3])
        self.store.record_routing(ch, 3, [])
        self.store.compact_routing(ws["id"], channel_ids)
        self.assertEqual(self.store.routing_high_water(ws["id"]), 8)
        self.assertEqual(self.store.unrouted_ids(ws["id"], channel_ids), [])
        self.assertEqual(self.store.get(ws["id"]).get("routing_done", []), [])

    def test_compaction_drops_done_ids_deleted_from_the_channel(self):
        ws = self.store.create("x")
        ch = ws["channel"]
        self.store.record_routing(ch, 4, [])      # a slash command: processed, then deleted from the store
        self.store.record_routing(ch, 5, [])
        self.store.compact_routing(ws["id"], [5])
        self.assertEqual(self.store.routing_high_water(ws["id"]), 5)
        self.assertEqual(self.store.get(ws["id"]).get("routing_done", []), [])

    def test_routing_recorded_and_acks_compact(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        ch = ws["channel"]
        for mid in (141, 143, 147):
            self.store.record_routing(ch, mid, [ag["agent_id"]])
        self.assertEqual(self.store.routed_ids_for(ws["id"], ag["agent_id"]), [141, 143, 147])
        self.store.ack(ws["id"], ag["agent_id"], [143, 150])
        got = self.store.get_agent(ws["id"], ag["agent_id"])
        self.assertEqual(got["read_mark"], -1)
        self.assertEqual(got["acked_above_mark"], [143])
        self.store.ack(ws["id"], ag["agent_id"], [141])
        got = self.store.get_agent(ws["id"], ag["agent_id"])
        self.assertEqual(got["read_mark"], 143)
        self.assertEqual(got["acked_above_mark"], [])
        self.assertEqual(self.store.routed_ids_for(ws["id"], ag["agent_id"]), [147])
        self.store.ack_by_name("claude-1", ch, [147])
        self.assertEqual(self.store.get_agent(ws["id"], ag["agent_id"])["read_mark"], 147)
        self.assertEqual(self.store.routing_for(ws["id"]), {})

    def test_member_names_includes_stopped_and_archived(self):
        ws = self.store.create("x")
        ag = self.add(ws)
        self.store.update_agent(ws["id"], ag["agent_id"], last_state="exited")
        self.assertEqual(self.store.member_names(), ["claude-1"])
        self.store.set_archived(ws["id"], True)
        self.assertEqual(self.store.member_names(), ["claude-1"])                       # still reserved
        self.assertEqual(self.store.member_names(include_archived=False), [])


if __name__ == "__main__":
    unittest.main()
