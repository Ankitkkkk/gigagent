"""Conformance tests for provider adapters (spec §6)."""
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

import providers
from providers.base import AmbiguousSessionId, LaunchContext, NullAdapter, ProviderAdapter
from providers.claude import ClaudeAdapter
from providers.codex import CodexAdapter, ORIGINATOR_ENV, originator_for
from _workspace_helpers import FakeClock, make_launch, write_rollout


def assert_adapter_conforms(tc: unittest.TestCase, adapter: ProviderAdapter):
    """Every adapter get_adapter can return must pass this."""
    tc.assertIsInstance(adapter.name, str)
    tc.assertTrue(adapter.name)
    tc.assertIsInstance(adapter.supports_resume, bool)
    tc.assertIsInstance(adapter.can_locate_transcripts, bool)
    sid = adapter.allocate_session_id()
    tc.assertTrue(sid is None or isinstance(sid, str))
    args = adapter.new_session_args(sid)
    tc.assertIsInstance(args, list)
    for a in args:
        tc.assertNotIn("{", a, "unformatted placeholder in new_session_args")
    if adapter.supports_resume:
        rargs = adapter.resume_args("abc", Path("/tmp"))
        tc.assertIsInstance(rargs, list)
        tc.assertTrue(any("abc" in a for a in rargs), "resume_args must carry the id")
    else:
        with tc.assertRaises(NotImplementedError):
            adapter.resume_args("abc", Path("/tmp"))
    tc.assertIsInstance(adapter.launch_env(make_launch()), dict)
    with tempfile.TemporaryDirectory() as tmp:
        tc.assertIsNone(adapter.locate_transcript("does-not-exist", Path(tmp)))
    cmd = adapter.summarizer_command(None, Path("/p"), Path("/o"), Path("/w"))
    tc.assertTrue(cmd is None or isinstance(cmd, list))


class NullAdapterTests(unittest.TestCase):
    def test_conforms_and_is_spawn_only(self):
        a = NullAdapter("kilo", {"command": "kilo"})
        assert_adapter_conforms(self, a)
        self.assertEqual(a.name, "kilo")
        self.assertFalse(a.supports_resume)
        self.assertFalse(a.can_locate_transcripts)
        self.assertIsNone(a.allocate_session_id())
        self.assertEqual(a.new_session_args(None), [])
        self.assertEqual(a.launch_env(make_launch()), {})
        self.assertIsNone(a.discover_session_id(make_launch(), timeout=0))
        self.assertIsNone(a.summarizer_command(None, Path("/p"), Path("/o"), Path("/w")))


class GetAdapterTests(unittest.TestCase):
    def test_unknown_provider_is_null_adapter(self):
        a = providers.get_adapter("kilo", {"command": "kilo"})
        self.assertIsInstance(a, NullAdapter)
        self.assertEqual(a.name, "kilo")
        self.assertEqual(a.agent_cfg, {"command": "kilo"})

    def test_explicit_adapter_module_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "acme_adapter.py").write_text(
                "from providers.base import ProviderAdapter\n"
                "class AcmeAdapter(ProviderAdapter):\n"
                "    name = 'acme'\n"
                "    supports_resume = True\n"
                "    def resume_args(self, session_id, cwd):\n"
                "        return ['--resume', session_id]\n"
            )
            sys.path.insert(0, tmp)
            try:
                a = providers.get_adapter("acme", {"adapter": "acme_adapter:AcmeAdapter", "command": "acme"})
            finally:
                sys.path.remove(tmp)
                sys.modules.pop("acme_adapter", None)
        self.assertEqual(a.name, "acme")
        assert_adapter_conforms(self, a)
        self.assertEqual(a.agent_cfg["command"], "acme")

    def test_bad_adapter_spec_raises(self):
        with self.assertRaises(ValueError):
            providers.get_adapter("x", {"adapter": "no-colon-here"})


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.a = ClaudeAdapter({"command": "claude"}, home=self.home)

    def test_conforms(self):
        assert_adapter_conforms(self, self.a)
        self.assertTrue(self.a.supports_resume)
        self.assertTrue(self.a.can_locate_transcripts)

    def test_allocates_uuid4_and_passes_it_on_launch(self):
        sid = self.a.allocate_session_id()
        self.assertEqual(uuid.UUID(sid).version, 4)
        self.assertEqual(self.a.new_session_args(sid), ["--session-id", sid])
        self.assertEqual(self.a.new_session_args(None), [])

    def test_resume_args(self):
        self.assertEqual(self.a.resume_args("abc", Path("/proj")), ["--resume", "abc"])

    def test_locate_transcript_globs_any_project_dir(self):
        sid = str(uuid.uuid4())
        proj = self.home / ".claude" / "projects" / "-home-me-proj"
        proj.mkdir(parents=True)
        (proj / f"{sid}.jsonl").write_text("{}\n")
        self.assertEqual(self.a.locate_transcript(sid, Path("/anything")), proj / f"{sid}.jsonl")
        self.assertIsNone(self.a.locate_transcript(str(uuid.uuid4()), Path("/anything")))

    def test_summarizer_is_tool_free(self):
        cmd = self.a.summarizer_command(None, Path("/p"), Path("/o"), Path("/w"))
        self.assertEqual(cmd[0], "claude")
        self.assertIn("-p", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        i = cmd.index("--tools")
        self.assertEqual(cmd[i + 1], "")
        self.assertIn("haiku", cmd)

    def test_registered_as_builtin(self):
        self.assertIsInstance(providers.get_adapter("claude", {"command": "claude"}), ClaudeAdapter)


class CodexAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.clock = FakeClock()
        self.a = CodexAdapter({"command": "codex"}, home=self.home,
                              sleep=self.clock.sleep, clock=self.clock)

    def test_conforms(self):
        assert_adapter_conforms(self, self.a)
        self.assertTrue(self.a.supports_resume)
        self.assertTrue(self.a.can_locate_transcripts)
        self.assertIsNone(self.a.allocate_session_id())

    def test_launch_env_is_launch_specific(self):
        l1 = make_launch(agent_id="ag_1", nonce="aaa")
        l2 = make_launch(agent_id="ag_1", nonce="bbb")
        self.assertEqual(self.a.launch_env(l1), {ORIGINATOR_ENV: "agentchattr:ag_1:aaa"})
        self.assertNotEqual(self.a.launch_env(l1), self.a.launch_env(l2))

    def test_discovers_exactly_its_own_rollout(self):
        launch = make_launch(agent_id="ag_1", nonce="aaa", cwd="/proj")
        write_rollout(self.home, "11111111-1111-4111-8111-111111111111", originator_for(launch), "/proj")
        write_rollout(self.home, "22222222-2222-4222-8222-222222222222", "codex-tui", "/proj")       # unrelated, same cwd
        write_rollout(self.home, "33333333-3333-4333-8333-333333333333", "agentchattr:ag_1:old", "/proj")  # earlier launch
        self.assertEqual(self.a.discover_session_id(launch, timeout=10),
                         "11111111-1111-4111-8111-111111111111")

    def test_two_concurrent_launches_same_cwd_each_find_their_own(self):
        l1 = make_launch(agent_id="ag_1", nonce="aaa", cwd="/proj")
        l2 = make_launch(agent_id="ag_2", nonce="bbb", cwd="/proj")
        write_rollout(self.home, "11111111-1111-4111-8111-111111111111", originator_for(l1), "/proj")
        write_rollout(self.home, "22222222-2222-4222-8222-222222222222", originator_for(l2), "/proj")
        self.assertEqual(self.a.discover_session_id(l1, 10), "11111111-1111-4111-8111-111111111111")
        self.assertEqual(self.a.discover_session_id(l2, 10), "22222222-2222-4222-8222-222222222222")

    def test_none_after_timeout(self):
        launch = make_launch(agent_id="ag_9", nonce="zzz")
        self.assertIsNone(self.a.discover_session_id(launch, timeout=5))
        self.assertGreaterEqual(self.clock.t, 5)

    def test_duplicate_originator_is_ambiguous(self):
        launch = make_launch(agent_id="ag_1", nonce="aaa")
        write_rollout(self.home, "11111111-1111-4111-8111-111111111111", originator_for(launch), "/proj")
        write_rollout(self.home, "22222222-2222-4222-8222-222222222222", originator_for(launch), "/proj")
        with self.assertRaises(AmbiguousSessionId) as cm:
            self.a.discover_session_id(launch, 10)
        self.assertEqual(cm.exception.count, 2)

    def test_resume_args_and_locate(self):
        self.assertEqual(self.a.resume_args("abc", Path("/proj")), ["resume", "-C", "/proj", "abc"])
        p = write_rollout(self.home, "abc", "x", "/proj")
        self.assertEqual(self.a.locate_transcript("abc", Path("/proj")), p)

    def test_summarizer_is_none_in_v1(self):
        self.assertIsNone(self.a.summarizer_command(None, Path("/p"), Path("/o"), Path("/w")))

    def test_registered_as_builtin(self):
        self.assertIsInstance(providers.get_adapter("codex", {"command": "codex"}), CodexAdapter)


if __name__ == "__main__":
    unittest.main()
