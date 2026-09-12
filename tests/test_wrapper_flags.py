"""Wrapper launch flags used by the workspace launcher (spec §2)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wrapper
import wrapper_unix


class ParseArgsTests(unittest.TestCase):
    def test_new_flags_and_passthrough(self):
        args, extra = wrapper.parse_wrapper_args(
            ["claude", "--no-attach", "--no-restart", "--cwd", "/proj",
             "--identity-file", "/id.json", "--tmux-name", "agentchattr-ag_1",
             "--provider-env", "A=1", "--provider-env", "B=x=y",
             "--session-id", "abc"], ["claude", "codex"])
        self.assertTrue(args.no_attach)
        self.assertEqual(args.cwd, "/proj")
        self.assertEqual(args.identity_file, "/id.json")
        self.assertEqual(args.tmux_name, "agentchattr-ag_1")
        self.assertEqual(wrapper.parse_provider_env(args.provider_env), {"A": "1", "B": "x=y"})
        self.assertEqual(extra, ["--session-id", "abc"])

    def test_positional_provider_args_pass_through(self):
        args, extra = wrapper.parse_wrapper_args(["codex", "--no-attach", "resume", "-C", "/p", "abc"], ["codex"])
        self.assertEqual(extra, ["resume", "-C", "/p", "abc"])

    def test_defaults_keep_old_behaviour(self):
        args, extra = wrapper.parse_wrapper_args(["claude"], ["claude"])
        self.assertFalse(args.no_attach)
        self.assertIsNone(args.cwd)
        self.assertIsNone(args.identity_file)
        self.assertIsNone(args.tmux_name)
        self.assertEqual(args.provider_env, [])


class IdentityFileTests(unittest.TestCase):
    def test_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "id.json"
            p.write_text(json.dumps({"registry_name": "claude-1", "token": "tok", "agent_id": "ag_1"}))
            ident = wrapper.load_identity_file(str(p))
            self.assertEqual(ident["name"], "claude-1")
            self.assertEqual(ident["token"], "tok")
            self.assertEqual(ident["slot"], 1)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            wrapper.load_identity_file("/nonexistent/id.json")


@unittest.skipIf(sys.platform == "win32", "tmux fixture")
class NoAttachTests(unittest.TestCase):
    def _fake_tmux(self, tmp, lines_of_output="hello\n"):
        calls = Path(tmp) / "calls"
        exe = Path(tmp) / "tmux"
        exe.write_text(
            "#!" + sys.executable + "\n"
            "import os, sys\n"
            "verb = sys.argv[1]\n"
            "open(os.environ['CALLS'], 'a').write(verb + '\\n')\n"
            "if verb == 'has-session':\n"
            "    n = sum(1 for l in open(os.environ['CALLS']) if l.strip() == 'has-session')\n"
            "    sys.exit(0 if n < 3 else 1)\n"   # alive for two polls, then gone
            "if verb == 'capture-pane': sys.stdout.write(os.environ.get('PANE', ''))\n"
        )
        exe.chmod(0o755)
        return calls

    def test_run_agent_without_attach_returns_when_session_dies(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = self._fake_tmux(tmp)
            env = {"PATH": tmp + os.pathsep + os.environ.get("PATH", ""), "CALLS": str(calls)}
            with mock.patch.dict(os.environ, env), mock.patch.object(wrapper_unix.time, "sleep", lambda s: None):
                wrapper_unix.run_agent(command="/bin/true", extra_args=[], cwd=tmp, env=dict(os.environ),
                                       queue_file=Path(tmp) / "q", agent="kilo", no_restart=True,
                                       start_watcher=lambda fn: None, session_name="agentchattr-ag_x",
                                       attach=False)
            verbs = calls.read_text().split()
            self.assertIn("new-session", verbs)
            self.assertNotIn("attach-session", verbs)

    def test_pane_has_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = self._fake_tmux(tmp)
            env = {"PATH": tmp + os.pathsep + os.environ.get("PATH", ""), "CALLS": str(calls), "PANE": ""}
            with mock.patch.dict(os.environ, env):
                self.assertFalse(wrapper_unix.pane_has_output("agentchattr-ag_x"))
            env["PANE"] = "> claude ready\n"
            with mock.patch.dict(os.environ, env):
                self.assertTrue(wrapper_unix.pane_has_output("agentchattr-ag_x"))


@unittest.skipIf(sys.platform == "win32", "Unix wrapper dispatch")
class MainFlagsTests(unittest.TestCase):
    def test_main_uses_handed_identity_and_one_resolved_project_directory(self):
        import config_loader
        import urllib.request
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            identity = root / "identity.json"
            identity.write_text(json.dumps({"registry_name": "claude-1", "token": "test-token", "agent_id": "ag_x"}))
            original = identity.read_bytes()
            cfg = {"server": {"data_dir": str(root / "data")}, "mcp": {},
                   "agents": {"claude": {"command": "/bin/true", "cwd": "/unused", "mcp_inject": "settings_file"}}}
            argv = ["wrapper.py", "claude", "--cwd", "project", "--identity-file", str(identity),
                    "--no-attach", "--no-restart", "--tmux-name", "agentchattr-ag_x",
                    "--provider-env", "ORIGIN=test"]
            with mock.patch.object(wrapper, "ROOT", root), mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(config_loader, "load_config", return_value=cfg), \
                 mock.patch.object(wrapper, "_register_instance", side_effect=AssertionError("unexpected registration")), \
                 mock.patch.object(wrapper, "_build_provider_launch", return_value=([], {}, {}, None)) as build, \
                 mock.patch.object(wrapper.threading.Thread, "start"), \
                 mock.patch.object(wrapper_unix, "run_agent") as run, \
                 mock.patch.object(urllib.request, "urlopen"), contextlib.redirect_stdout(io.StringIO()):
                wrapper.main()
            self.assertEqual(build.call_args.kwargs["project_dir"], project)
            self.assertEqual(run.call_args.kwargs["cwd"], str(project))
            self.assertEqual(build.call_args.kwargs["instance_name"], "claude-1")
            self.assertFalse(run.call_args.kwargs["attach"])
            self.assertEqual(run.call_args.kwargs["session_name"], "agentchattr-ag_x")
            self.assertEqual(run.call_args.kwargs["inject_env"]["ORIGIN"], "test")
            self.assertEqual(identity.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
