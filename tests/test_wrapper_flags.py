"""Wrapper launch flags used by the workspace launcher (spec §2)."""
import contextlib
import io
import json
import os
import sys
from types import SimpleNamespace
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
    def test_separator_protects_provider_flags_from_wrapper_parser(self):
        args, extra = wrapper.parse_wrapper_args(
            ['codex', '--cwd', '/managed', '--port', '8300', '--',
             '--port', '9000', '--label', 'provider label', '--cwd', '/provider'], ['codex'])
        self.assertEqual(args.cwd, '/managed')
        self.assertEqual(args.port, '8300')
        self.assertIsNone(args.label)
        self.assertEqual(extra, ['--port', '9000', '--label', 'provider label', '--cwd', '/provider'])

    def test_new_flags_and_passthrough(self):
        args, extra = wrapper.parse_wrapper_args(
            ["claude", "--no-attach", "--no-restart", "--cwd", "/proj",
             "--identity-file", "/id.json", "--tmux-name", "yapp-ag_1",
             "--provider-env", "A=1", "--provider-env", "B=x=y",
             "--session-id", "abc"], ["claude", "codex"])
        self.assertTrue(args.no_attach)
        self.assertEqual(args.cwd, "/proj")
        self.assertEqual(args.identity_file, "/id.json")
        self.assertEqual(args.tmux_name, "yapp-ag_1")
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

    def test_non_object_json_exits_with_identity_error(self):
        for payload in (["claude-1", "tok"], None, "claude-1"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "id.json"
                p.write_text(json.dumps(payload))
                with self.assertRaisesRegex(SystemExit, r"identity file .* lacks registry_name/token"):
                    wrapper.load_identity_file(str(p))


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
                                       start_watcher=lambda fn: None, session_name="yapp-ag_x",
                                       attach=False)
            verbs = calls.read_text().split()
            self.assertIn("new-session", verbs)
            self.assertNotIn("attach-session", verbs)

    def test_pane_has_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = self._fake_tmux(tmp)
            env = {"PATH": tmp + os.pathsep + os.environ.get("PATH", ""), "CALLS": str(calls), "PANE": ""}
            with mock.patch.dict(os.environ, env):
                self.assertFalse(wrapper_unix.pane_has_output("yapp-ag_x"))
            env["PANE"] = "> claude ready\n"
            with mock.patch.dict(os.environ, env):
                self.assertTrue(wrapper_unix.pane_has_output("yapp-ag_x"))


@unittest.skipIf(sys.platform == "win32", "Unix wrapper dispatch")
class MainFlagsTests(unittest.TestCase):
    def test_codex_hook_stream_is_wired_to_checker_and_cleaned_after_launch(self):
        import config_loader
        import urllib.request
        from waiting_hooks import EVENTS_ENV, emit
        from tests.test_prompt_signals import MCP_PROMPT, payload
        actual_checker = wrapper_unix.get_activity_checker
        checkers, paths = [], []
        def checker_factory(*args, **kwargs):
            checker = actual_checker(*args, **kwargs)
            checkers.append(checker)
            return checker
        def run_provider(**kwargs):
            path = Path(kwargs['inject_env'][EVENTS_ENV])
            paths.append(path)
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            emit('codex', payload(), path)
            captures = [SimpleNamespace(returncode=0, stdout=frame)
                        for frame in (b'Unknown approval', MCP_PROMPT, b'Working')]
            with mock.patch.object(wrapper_unix.subprocess, 'run', side_effect=captures):
                for expected in (True, True, False):
                    checkers[-1]()
                    self.assertEqual(checkers[-1].waiting_for_input, expected)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / 'identity.json'
            identity.write_text(json.dumps({'registry_name': 'codex-1', 'token': 'test-token'}))
            cfg = {'server': {'data_dir': str(root / 'data')}, 'mcp': {},
                   'agents': {'codex': {'command': 'codex', 'cwd': str(root), 'mcp_inject': 'settings_file'}}}
            argv = ['wrapper.py', 'codex', '--identity-file', str(identity), '--no-attach', '--no-restart']
            with mock.patch.object(wrapper, 'ROOT', root), mock.patch.object(sys, 'argv', argv), \
                    mock.patch.object(config_loader, 'load_config', return_value=cfg), \
                    mock.patch.object(wrapper.shutil, 'which', return_value='/fake/codex'), \
                    mock.patch.object(wrapper, '_build_provider_launch', return_value=([], {}, {}, None)), \
                    mock.patch.object(wrapper.threading.Thread, 'start'), \
                    mock.patch.object(wrapper_unix, 'get_activity_checker', side_effect=checker_factory), \
                    mock.patch.object(wrapper_unix, 'run_agent', side_effect=run_provider), \
                    mock.patch.object(urllib.request, 'urlopen'), contextlib.redirect_stdout(io.StringIO()):
                wrapper.main()
            self.assertEqual(len(paths), 1)
            self.assertFalse(paths[0].exists())

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
                    "--no-attach", "--no-restart", "--tmux-name", "yapp-ag_x",
                    "--provider-env", "ORIGIN=test"]
            with mock.patch.object(wrapper, "ROOT", root), mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(config_loader, "load_config", return_value=cfg), \
                 mock.patch.object(wrapper, "_register_instance", side_effect=AssertionError("unexpected registration")), \
                 mock.patch.object(wrapper, "_build_provider_launch", return_value=([], {}, {}, None)) as build, \
                 mock.patch.object(wrapper.threading.Thread, "start", autospec=True) as started, \
                 mock.patch.object(wrapper_unix, "run_agent") as run, \
                 mock.patch.object(urllib.request, "urlopen") as http, contextlib.redirect_stdout(io.StringIO()):
                wrapper.main()
                monitor = next(call.args[0]._target for call in started.call_args_list
                               if call.args[0]._target.__name__ == '_activity_monitor')
                # A hint transition must report immediately even if active stays
                # true and the periodic refresh interval has not elapsed.
                frames = [b'Idle', b'Continue? [y/N]', b'Working', b'Working']
                captures = [SimpleNamespace(returncode=0, stdout=frame) for frame in frames]
                clock = SimpleNamespace(time=lambda: 100,
                    sleep=mock.Mock(side_effect=[None] * 4 + [KeyboardInterrupt]))
                http.reset_mock()
                with mock.patch.object(wrapper, 'time', clock), \
                        mock.patch.object(wrapper_unix.subprocess, 'run', side_effect=captures):
                    with self.assertRaises(KeyboardInterrupt):
                        monitor()
                payloads = [json.loads(call.args[0].data) for call in http.call_args_list]
                self.assertEqual(payloads, [
                    {'active': False, 'waiting_for_input': False},
                    {'active': True, 'waiting_for_input': True},
                    {'active': True, 'waiting_for_input': False},
                    {'active': False, 'waiting_for_input': False}])
            self.assertEqual(build.call_args.kwargs["project_dir"], project)
            self.assertEqual(run.call_args.kwargs["cwd"], str(project))
            self.assertEqual(build.call_args.kwargs["instance_name"], "claude-1")
            self.assertFalse(run.call_args.kwargs["attach"])
            self.assertEqual(run.call_args.kwargs["session_name"], "yapp-ag_x")
            self.assertEqual(run.call_args.kwargs["inject_env"]["ORIGIN"], "test")
            self.assertEqual(identity.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
