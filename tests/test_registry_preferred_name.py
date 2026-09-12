"""registry.register(preferred_name=...) and free_slot_name (spec §1, §2)."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from registry import NameInUse, RuntimeRegistry


class PreferredNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reg = RuntimeRegistry(data_dir=self.tmp)
        self.reg.seed({
            "claude": {"label": "Claude", "color": "#ff6a00"},
            "codex": {"label": "Codex", "color": "#00B67D"},
        })

    def test_free_slot_name_counts_from_one(self):
        self.assertEqual(self.reg.free_slot_name("claude"), "claude-1")
        self.reg.register("claude", preferred_name="claude-1")
        self.assertEqual(self.reg.free_slot_name("claude"), "claude-2")

    def test_free_slot_name_skips_excluded_saved_names(self):
        self.assertEqual(self.reg.free_slot_name("claude", exclude={"claude-1", "claude-2"}), "claude-3")

    def test_family_preferred_name_takes_that_slot(self):
        r = self.reg.register("claude", label="ws claude", preferred_name="claude-2")
        self.assertEqual(r["name"], "claude-2")
        self.assertEqual(r["slot"], 2)
        self.assertEqual(r["label"], "ws claude")
        self.assertIn("token", r)
        self.assertEqual(self.reg.get_instance("claude-2")["state"], "active")

    def test_slot_one_named_claude_1_not_bare(self):
        r = self.reg.register("claude", preferred_name="claude-1")
        self.assertEqual(r["name"], "claude-1")
        self.assertIsNone(self.reg.get_instance("claude"))

    def test_taken_name_raises(self):
        self.reg.register("claude", preferred_name="claude-1")
        with self.assertRaises(NameInUse) as cm:
            self.reg.register("claude", preferred_name="claude-1")
        self.assertEqual(cm.exception.name, "claude-1")

    def test_bare_slot_one_blocks_claude_1(self):
        self.reg.register("claude")  # non-workspace agent holds slot 1 as "claude"
        with self.assertRaises(NameInUse):
            self.reg.register("claude", preferred_name="claude-1")
        self.assertEqual(self.reg.free_slot_name("claude"), "claude-2")

    def test_reserved_name_after_deregister_raises_until_grace(self):
        self.reg.register("claude", preferred_name="claude-1")
        self.reg.deregister("claude-1")
        with self.assertRaises(NameInUse):
            self.reg.register("claude", preferred_name="claude-1")

    def test_owner_may_reclaim_reserved_name(self):
        self.reg.register("claude", preferred_name="claude-1")
        self.reg.deregister("claude-1")
        r = self.reg.register("claude", preferred_name="claude-1", allow_reserved=True)
        self.assertEqual(r["name"], "claude-1")
        self.assertEqual(self.reg.free_slot_name("claude"), "claude-2")

    def test_custom_name_registers_then_renames(self):
        r = self.reg.register("claude", label="Reviewer", preferred_name="reviewer")
        self.assertEqual(r["name"], "reviewer")
        self.assertEqual(r["base"], "claude")
        self.assertIn("token", r)
        self.assertEqual(self.reg.resolve_token(r["token"])["name"], "reviewer")
        self.assertEqual(self.reg.resolve_to_instances("claude"), ["reviewer"])

    def test_custom_name_in_other_family_raises_and_leaves_nothing(self):
        with self.assertRaises(NameInUse):
            self.reg.register("claude", preferred_name="codex-3")
        self.assertEqual(self.reg.get_all_names(), [])

    def test_no_preferred_name_is_unchanged_behaviour(self):
        r = self.reg.register("claude")
        self.assertEqual(r["name"], "claude")


if __name__ == "__main__":
    unittest.main()
