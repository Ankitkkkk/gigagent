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
from providers.base import LaunchContext, NullAdapter, ProviderAdapter
from providers.claude import ClaudeAdapter
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


if __name__ == "__main__":
    unittest.main()
