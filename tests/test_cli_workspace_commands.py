"""Parser and shell command contracts for terminal sessions (spec §5)."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli
import cli_workspaces
from cli_api import CLIError


WINDOWS_ERROR = "Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch."
SUMMARY_ERROR = "summary history mode is not available in this version; use literal or none"


def workspace(*, name="billing", archived=False):
    return {
        "id": "ws_a",
        "name": name,
        "channel": "ws-a",
        "archived": archived,
        "created_at": "2026-09-12T10:00:00+00:00",
        "updated_at": "2026-09-12T11:00:00+00:00",
        "agents": [{
            "agent_id": "ag_a",
            "registry_name": "claude-1",
            "provider": "claude",
            "cwd": "/tmp/project",
            "history_mode": "none",
            "history_state": "done",
            "floor_id": 0,
            "last_state": "running",
            "native_session_id": "native-a",
            "unread_count": 2,
        }],
    }


def agent(*, state="running"):
    return {
        "agent_id": "ag_a",
        "registry_name": "claude-1",
        "provider": "claude",
        "cwd": "/tmp/project",
        "history_mode": "none",
        "history_state": "done",
        "floor_id": 0,
        "last_state": state,
        "native_session_id": "native-a",
        "unread_count": 2,
    }


class RecordingAPI:
    def __init__(self, selected=None):
        self.selected = selected or workspace()
        self.calls = []
        self.responses = {}

    def list(self, include_archived=False):
        self.calls.append(("list", include_archived))
        return self.responses.get("list", {"workspaces": [self.selected]})

    def resolve(self, selector, include_archived=True):
        self.calls.append(("resolve", selector, include_archived))
        if selector not in (self.selected["id"], self.selected["name"], self.selected["name"][:4]):
            raise CLIError(f"session not found: {selector}")
        return self.selected

    def create(self, name=""):
        self.calls.append(("create", name))
        return self.responses.get("create", self.selected)

    def action(self, ws_id, action_name, agent_id=None, body=None):
        self.calls.append(("action", ws_id, action_name, agent_id, body))
        return self.responses.get(action_name,
                                  {"ok": True} if action_name == "retry" else agent())

    def unread(self, ws_id, agent_id=None):
        self.calls.append(("unread", ws_id, agent_id))
        return self.responses.get("unread", {"agents": [{
            "agent_id": "ag_a",
            "registry_name": "claude-1",
            "read_mark": 16,
            "acked_above_mark": [],
            "floor_id": 0,
            "count": 1,
            "messages": [{
                "id": 17,
                "sender": "Pat",
                "time": "11:00",
                "text": "please inspect",
                "routed_to": ["ag_a"],
            }],
        }]})


class TerminalInput(StringIO):
    def __init__(self, value="", *, tty=False):
        super().__init__(value)
        self._tty = tty

    def isatty(self):
        return self._tty


class ParserTests(unittest.TestCase):
    def parse(self, argv):
        return cli.build_parser().parse_args(argv)

    def test_common_options_work_before_and_after_command(self):
        cases = [
            ["--url", "http://localhost:9000", "--session", "billing",
             "--name", "Pat", "--history", "41", "--timeout", "9", "--json", "read"],
            ["read", "--url", "http://localhost:9000", "--session", "billing",
             "--name", "Pat", "--history", "41", "--timeout", "9", "--json"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                args = self.parse(argv)
                self.assertEqual(args.url, "http://localhost:9000")
                self.assertEqual(args.session, "billing")
                self.assertIsNone(args.channel)
                self.assertEqual(args.name, "Pat")
                self.assertEqual(args.history, 41)
                self.assertEqual(args.timeout, 9)
                self.assertTrue(args.json)

    def test_channel_option_works_at_either_parser_level(self):
        for argv in (["--channel", "work", "read"], ["read", "--channel", "work"]):
            with self.subTest(argv=argv):
                args = self.parse(argv)
                self.assertEqual(args.channel, "work")
                self.assertIsNone(args.session)

    def test_spawn_uses_explicit_agent_and_history_option_names(self):
        args = self.parse(["--session", "billing", "spawn", "claude",
                           "--cwd", "/tmp/project", "--agent-name", "reviewer",
                           "--history-mode", "none"])
        self.assertEqual(args.command, "spawn")
        self.assertEqual(args.provider, "claude")
        self.assertEqual(args.cwd, "/tmp/project")
        self.assertEqual(args.history, 30)
        self.assertEqual(args.history_mode, "none")
        self.assertEqual(args.agent_name, "reviewer")
        self.assertIsNone(args.name)

    def test_command_positionals_do_not_overwrite_human_name_or_session(self):
        created = self.parse(["--name", "Pat", "new", "billing"])
        archived = self.parse(["--name", "Pat", "archive", "billing", "--yes"])
        self.assertEqual(created.name, "Pat")
        self.assertEqual(created.session_name, "billing")
        self.assertEqual(archived.name, "Pat")
        self.assertEqual(archived.target_session, "billing")

    def test_all_non_attach_session_command_shapes(self):
        cases = {
            "sessions": ["sessions", "--archived"],
            "new": ["new", "billing"],
            "spawn": ["spawn", "claude", "--session", "billing", "--cwd", "/tmp/project"],
            "resume": ["resume", "claude-1", "--session", "billing", "--fresh",
                       "--agent-name", "reviewer", "--cwd", "/tmp/new"],
            "stop": ["stop", "claude-1", "--session", "billing"],
            "unread": ["unread", "--session", "billing", "--agent", "claude-1"],
            "retry": ["retry", "claude-1", "--session", "billing"],
            "history": ["history", "claude-1", "--session", "billing",
                        "--mode", "literal"],
            "archive": ["archive", "billing", "--yes"],
        }
        for command, argv in cases.items():
            with self.subTest(command=command):
                self.assertEqual(self.parse(argv).command, command)

    def test_chat_has_session_and_no_resume_options(self):
        args = self.parse(["chat", "--session", "billing", "--no-resume"])
        self.assertEqual(args.session, "billing")
        self.assertTrue(args.no_resume)

    def test_common_defaults_remain_compatible(self):
        args = self.parse(["read"])
        self.assertIsNone(args.url)
        self.assertIsNone(args.channel)
        self.assertIsNone(args.session)
        self.assertIsNone(args.name)
        self.assertEqual(args.history, 30)
        self.assertEqual(args.timeout, 15)
        self.assertFalse(args.json)

    def test_checkpoint_is_not_a_public_command(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            self.parse(["checkpoint", "--session", "billing"])


class DispatchTests(unittest.TestCase):
    def runner(self):
        value = getattr(cli_workspaces, "run_workspace_command", None)
        if value is None:
            self.fail("run_workspace_command is missing")
        return value

    def parse(self, argv):
        return cli.build_parser().parse_args(argv)

    def test_sessions_and_new_dispatch_exact_calls(self):
        api = RecordingAPI()
        sessions = self.runner()(api, self.parse(["sessions", "--archived"]))
        created = self.runner()(api, self.parse(["new", "billing"]))
        self.assertEqual(api.calls, [("list", True), ("create", "billing")])
        self.assertEqual(sessions.data, {"workspaces": [api.selected]})
        self.assertEqual(created.data, api.selected)
        self.assertIsNone(sessions.workspace)
        self.assertIsNone(created.workspace)

    def test_spawn_dispatches_exact_body(self):
        api = RecordingAPI()
        args = self.parse(["spawn", "claude", "--session", "billing",
                           "--cwd", "/tmp/project", "--agent-name", "reviewer",
                           "--history-mode", "none"])
        with patch.object(cli_workspaces, "os", SimpleNamespace(name="posix"), create=True):
            result = self.runner()(api, args)
        self.assertEqual(api.calls, [
            ("resolve", "billing", True),
            ("action", "ws_a", "spawn", None, {
                "provider": "claude", "cwd": "/tmp/project",
                "history_mode": "none", "name": "reviewer",
            }),
        ])
        self.assertEqual(result.data["registry_name"], "claude-1")
        self.assertEqual(result.workspace, api.selected)

    def test_resume_dispatches_exact_agent_and_body(self):
        api = RecordingAPI()
        args = self.parse(["resume", "claude", "--session", "billing", "--fresh",
                           "--agent-name", "replacement", "--cwd", "/tmp/new"])
        with patch.object(cli_workspaces, "os", SimpleNamespace(name="posix"), create=True):
            self.runner()(api, args)
        self.assertEqual(api.calls, [
            ("resolve", "billing", True),
            ("action", "ws_a", "resume", "ag_a", {
                "fresh": True, "name": "replacement", "cwd": "/tmp/new",
            }),
        ])

    def test_stop_retry_and_history_dispatch_exact_calls(self):
        api = RecordingAPI()
        self.runner()(api, self.parse(["stop", "claude-1", "--session", "billing"]))
        self.runner()(api, self.parse(["retry", "ag_a", "--session", "billing"]))
        self.runner()(api, self.parse(["history", "claude", "--session", "billing",
                                       "--mode", "literal"]))
        self.assertEqual(api.calls, [
            ("resolve", "billing", True),
            ("action", "ws_a", "stop", "ag_a", None),
            ("resolve", "billing", True),
            ("action", "ws_a", "retry", "ag_a", None),
            ("resolve", "billing", True),
            ("action", "ws_a", "history", "ag_a", {"mode": "literal"}),
        ])

    def test_unread_resolves_optional_agent_and_reuses_workspace_snapshot(self):
        api = RecordingAPI()
        result = self.runner()(api, self.parse(
            ["unread", "--session", "billing", "--agent", "claude"]))
        self.assertEqual(api.calls, [
            ("resolve", "billing", True),
            ("unread", "ws_a", "ag_a"),
        ])
        self.assertIs(result.workspace, api.selected)
        self.assertEqual(result.data["agents"][0]["count"], 1)

    def test_archive_uses_positional_target_without_checkpoint(self):
        api = RecordingAPI()
        result = self.runner()(api, self.parse(["archive", "billing", "--yes"]))
        self.assertEqual(api.calls, [
            ("resolve", "billing", True),
            ("action", "ws_a", "archive", None, None),
        ])
        self.assertEqual(result.workspace, api.selected)
        self.assertNotIn("checkpoint", repr(api.calls))

    def test_unknown_session_stops_before_mutation(self):
        api = RecordingAPI()
        with self.assertRaisesRegex(CLIError, "session not found: missing"):
            self.runner()(api, self.parse(
                ["stop", "claude", "--session", "missing"]))
        self.assertEqual(api.calls, [("resolve", "missing", True)])

    def test_ambiguous_agent_stops_before_mutation(self):
        selected = workspace()
        selected["agents"].append(dict(selected["agents"][0],
                                       agent_id="ag_b", registry_name="claude-2"))
        api = RecordingAPI(selected)
        with self.assertRaisesRegex(CLIError, r"ambiguous agent claude:.*ag_a.*ag_b"):
            self.runner()(api, self.parse(
                ["stop", "claude", "--session", "billing"]))
        self.assertEqual(api.calls, [("resolve", "billing", True)])

    def test_windows_spawn_and_resume_fail_before_launch_action(self):
        guard = getattr(cli_workspaces, "require_tmux_platform", None)
        if guard is None:
            self.fail("require_tmux_platform is missing")
        api = RecordingAPI()
        with patch.object(cli_workspaces, "os", SimpleNamespace(name="nt"), create=True):
            with self.assertRaisesRegex(CLIError, r"^" + WINDOWS_ERROR.replace("(", r"\(").replace(")", r"\)") + r"$"):
                guard()
            for argv in (["spawn", "claude", "--session", "billing", "--cwd", "/tmp"],
                         ["resume", "claude", "--session", "billing"]):
                with self.subTest(argv=argv), self.assertRaisesRegex(CLIError, "Requires tmux"):
                    self.runner()(api, self.parse(argv))
        self.assertFalse(any(call[0] == "action" for call in api.calls))


class FormattingTests(unittest.TestCase):
    def result(self, data, selected=None):
        cls = getattr(cli_workspaces, "WorkspaceCommandResult", None)
        if cls is None:
            self.fail("WorkspaceCommandResult is missing")
        return cls(data, selected)

    def formatter(self):
        value = getattr(cli_workspaces, "format_workspace_result", None)
        if value is None:
            self.fail("format_workspace_result is missing")
        return value

    def test_sessions_new_and_lifecycle_human_output(self):
        selected = workspace()
        sessions = self.formatter()("sessions", self.result({"workspaces": [selected]}))
        created = self.formatter()("new", self.result(selected))
        lifecycle = self.formatter()("spawn", self.result(agent(), selected))
        for text in (sessions, created):
            self.assertIn("billing", text)
            self.assertIn("ws_a", text)
        self.assertIn("claude-1", sessions)
        self.assertIn("running", sessions)
        self.assertIn("unread 2", sessions)
        self.assertIn("ws-a", created)
        self.assertIn("claude-1", lifecycle)
        self.assertIn("running", lifecycle)

    def test_unread_human_output_uses_snapshot_state_and_message_rows(self):
        selected = workspace()
        data = RecordingAPI(selected).unread("ws_a")
        text = self.formatter()("unread", self.result(data, selected))
        self.assertIn("claude-1", text)
        self.assertIn("running", text)
        self.assertIn("1", text)
        self.assertIn("#17 Pat please inspect", text)

    def test_retry_success_is_explicit(self):
        text = self.formatter()("retry", self.result({"ok": True}, workspace()))
        self.assertIn("retry", text.lower())
        self.assertTrue(any(word in text.lower() for word in ("requested", "successful", "succeeded")))


class MainValidationTests(unittest.TestCase):
    def run_main(self, argv, *, tty=False, input_text="", api=None, runner=None):
        stdout = StringIO()
        stderr = StringIO()
        api = api or RecordingAPI()
        runner = runner or MagicMock(return_value=SimpleNamespace(data={}, workspace=None))
        with patch.object(cli.sys, "stdin", TerminalInput(input_text, tty=tty)), \
                patch.object(cli, "WorkspaceAPI", return_value=api, create=True) as api_type, \
                patch.object(cli, "run_workspace_command", runner, create=True), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                cli.main(argv)
                code = 0
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue(), api_type, runner

    def test_channel_and_session_conflict_fails_before_network_across_levels(self):
        cases = [
            ["--channel", "work", "send", "--session", "billing", "hello"],
            ["--session", "billing", "send", "--channel", "work", "hello"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _, stderr, api_type, runner = self.run_main(argv)
                self.assertNotEqual(code, 0)
                self.assertIn("--channel", stderr)
                self.assertIn("--session", stderr)
                api_type.assert_not_called()
                runner.assert_not_called()

    def test_history_and_timeout_ranges_fail_before_network(self):
        for argv, expected in [(["read", "--history", "0"], "--history"),
                               (["read", "--history", "10001"], "--history"),
                               (["sessions", "--timeout", "0"], "--timeout"),
                               (["sessions", "--timeout", "301"], "--timeout")]:
            with self.subTest(argv=argv):
                code, _, stderr, api_type, runner = self.run_main(argv)
                self.assertNotEqual(code, 0)
                self.assertIn(expected, stderr)
                api_type.assert_not_called()
                runner.assert_not_called()

    def test_required_session_and_archive_target_ambiguity_fail_before_network(self):
        for argv in (["stop", "claude"],
                     ["archive", "billing", "--session", "other", "--yes"]):
            with self.subTest(argv=argv):
                code, _, stderr, api_type, runner = self.run_main(argv)
                self.assertNotEqual(code, 0)
                self.assertIn("session", stderr.lower())
                api_type.assert_not_called()
                runner.assert_not_called()

    def test_summary_mode_is_rejected_before_api(self):
        code, _, stderr, api_type, runner = self.run_main(
            ["history", "claude", "--session", "billing", "--mode", "summary"])
        self.assertNotEqual(code, 0)
        self.assertIn(SUMMARY_ERROR, stderr)
        api_type.assert_not_called()
        runner.assert_not_called()

    def test_archive_requires_yes_without_terminal(self):
        code, _, stderr, api_type, runner = self.run_main(
            ["archive", "billing"], tty=False)
        self.assertEqual(code, 1)
        self.assertIn("--yes", stderr)
        api_type.assert_not_called()
        runner.assert_not_called()

    def test_archive_terminal_confirmation_defaults_no(self):
        code, stdout, _, _, runner = self.run_main(
            ["archive", "billing"], tty=True, input_text="\n")
        self.assertEqual(code, 0)
        self.assertIn("[y/N]", stdout)
        runner.assert_not_called()

    def test_archive_terminal_confirmation_yes_dispatches(self):
        code, stdout, stderr, _, runner = self.run_main(
            ["archive", "billing"], tty=True, input_text="y\n")
        self.assertEqual(code, 0, stderr)
        self.assertIn("[y/N]", stdout)
        runner.assert_called_once()

    def test_http_400_and_409_errors_are_verbatim_on_stderr(self):
        for status, message in [(400, "session is archived; unarchive it first"),
                                (409, "catch-up in progress")]:
            runner = MagicMock(side_effect=CLIError(message, status))
            with self.subTest(status=status):
                code, stdout, stderr, _, _ = self.run_main(
                    ["new", "billing", "--json"], runner=runner)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, message + "\n")

    def test_json_serializes_original_api_data_exactly(self):
        payload = {"name": "billing\x1b[31m", "agents": [], "unicode": "café"}
        runner = MagicMock(return_value=SimpleNamespace(data=payload, workspace=None))
        code, stdout, stderr, _, _ = self.run_main(
            ["new", "billing", "--json"], runner=runner)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), payload)

    def test_shell_connection_failure_includes_manual_start_hint(self):
        runner = MagicMock(side_effect=CLIError(
            "Could not connect to the local agentchattr server"))
        code, _, stderr, _, _ = self.run_main(["sessions"], runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("Could not connect", stderr)
        self.assertIn("Start it manually: python run.py", stderr)

    def test_shell_timeout_includes_manual_start_hint(self):
        runner = MagicMock(side_effect=TimeoutError)
        code, _, stderr, _, _ = self.run_main(["sessions"], runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("timed out", stderr)
        self.assertIn("Start it manually: python run.py", stderr)


class SessionChannelCompatibilityTests(unittest.TestCase):
    def run_shell(self, argv, result):
        captured = []

        async def shell(client, args):
            captured.append((client.channel, args.channel, args.session, args.command))
            return result

        api = RecordingAPI()
        stdout = StringIO()
        stderr = StringIO()
        with patch.object(cli, "WorkspaceAPI", return_value=api, create=True), \
                patch.object(cli, "shell_command", AsyncMock(side_effect=shell)), \
                patch.object(cli.sys, "stdin", TerminalInput()), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                cli.main(argv)
                code = 0
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue(), captured, api

    def test_session_send_resolves_channel_and_preserves_acknowledgment(self):
        code, stdout, stderr, captured, api = self.run_shell(
            ["send", "--session", "billing", "hello"], {"id": 73})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured, [("ws-a", "ws-a", "billing", "send")])
        self.assertEqual(stdout, "Sent to #ws-a (message 73)\n")
        self.assertEqual(api.calls, [("resolve", "billing", True)])

    def test_session_read_resolves_channel_and_preserves_read_transport(self):
        messages = [{"id": 7, "sender": "Pat", "text": "hello", "channel": "ws-a",
                     "timestamp": 7, "time": "11:00"}]
        code, stdout, stderr, captured, api = self.run_shell(
            ["read", "--session", "billing"], messages)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured, [("ws-a", "ws-a", "billing", "read")])
        self.assertIn("Pat: hello", stdout)
        self.assertEqual(api.calls, [("resolve", "billing", True)])

    def test_plain_send_still_defaults_to_general_without_workspace_api(self):
        code, stdout, stderr, captured, api = self.run_shell(["send", "hello"], {"id": 74})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured, [("general", "general", None, "send")])
        self.assertEqual(stdout, "Sent to #general (message 74)\n")
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
