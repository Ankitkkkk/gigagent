"""Parser and shell command contracts for terminal sessions (spec §5)."""

import argparse
import builtins
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli
import cli_api
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


class AttachHelperTests(unittest.TestCase):
    def helper(self, name='attach_agent'):
        helper = getattr(cli_workspaces, name, None)
        self.assertTrue(callable(helper), f'{name} is missing')
        return helper

    def test_stable_id_target_ignores_unusual_registry_name(self):
        self.assertEqual(self.helper('tmux_target')({
            'agent_id': 'ag_a', 'registry_name': 'name ; $(touch nope)'}),
            'yapp-ag_a')

    def test_present_server_target_does_not_fall_back(self):
        target = self.helper('tmux_target')
        self.assertEqual(target({'agent_id': 'ag_a', 'tmux_session': 'server-target'}),
                         'server-target')
        for value in ('', None):
            with self.subTest(value=value), self.assertRaises(CLIError):
                target({'agent_id': 'ag_a', 'tmux_session': value})

    def test_foreground_attach_inherits_stdio_and_environment_without_deadline(self):
        runner = MagicMock(side_effect=[subprocess.CompletedProcess([], 0),
                                       subprocess.CompletedProcess([], 7)])
        with patch.dict(os.environ, {'TMUX_TMPDIR': '/tmp/isolated'}, clear=True):
            code = self.helper()({'agent_id': 'ag_a'}, runner=runner)
        self.assertEqual(code, 7)
        self.assertEqual(runner.call_args_list[0].args[0],
                         ['tmux', 'has-session', '-t', '=yapp-ag_a'])
        self.assertEqual(runner.call_args_list[0].kwargs,
                         {'timeout': 5, 'capture_output': True})
        self.assertEqual(runner.call_args_list[1].args[0],
                         ['tmux', 'attach', '-t', 'yapp-ag_a'])
        self.assertEqual(runner.call_args_list[1].kwargs, {})
        self.assertEqual(runner.call_count, 2)

    def test_server_target_and_nested_switch_emit_switch_back_hint(self):
        output = []
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 0))
        with patch.dict(os.environ, {'TMUX': 'inside'}):
            code = self.helper()({'agent_id': 'ag_a', 'tmux_session': 'server-target'},
                                 runner=runner, output=output.append)
        self.assertEqual(code, 0)
        self.assertEqual(runner.call_args_list[0].args[0],
                         ['tmux', 'has-session', '-t', '=server-target'])
        self.assertEqual(runner.call_args.args[0],
                         ['tmux', 'switch-client', '-t', 'server-target'])
        self.assertEqual(output, ['Switch back: tmux switch-client -l'])

    def test_missing_session_has_chat_or_shell_resume_hint(self):
        attach = self.helper()
        for session, hint in [(None, '/resume claude-1'),
                              ('ws_a', 'python cli.py resume claude-1 --session ws_a')]:
            runner = MagicMock(return_value=subprocess.CompletedProcess([], 1,
                                                       stderr='secret-token'))
            with self.subTest(session=session), patch.object(
                    sys, 'stdin', TerminalInput(tty=True)), patch.object(
                    sys, 'stdout', TerminalInput(tty=True)):
                with self.assertRaises(CLIError) as caught:
                    attach(agent(), runner=runner, shell_session=session)
            self.assertEqual(str(caught.exception), 'not running; resume with ' + hint)
            self.assertEqual(runner.call_count, 1)

    def test_windows_rejected_before_tmux(self):
        runner = MagicMock()
        with patch.object(cli_workspaces, 'os', SimpleNamespace(name='nt')):
            with self.assertRaisesRegex(CLIError, 'Requires tmux'):
                self.helper()(agent(), runner=runner)
        runner.assert_not_called()

    def test_non_tty_shell_attach_rejected_before_tmux(self):
        runner = MagicMock()
        with patch.object(sys, 'stdin', TerminalInput(tty=False)):
            with self.assertRaisesRegex(CLIError, 'requires a terminal'):
                self.helper()(agent(), runner=runner, shell_session='ws_a')
        runner.assert_not_called()

    def test_runner_errors_never_expose_diagnostics(self):
        attach = self.helper()
        for error in (OSError('secret-token'), subprocess.TimeoutExpired('secret-token', 5)):
            for stage in ('probe', 'attach'):
                runner = MagicMock(side_effect=([error] if stage == 'probe' else
                    [subprocess.CompletedProcess([], 0), error]))
                with self.subTest(error=type(error).__name__, stage=stage):
                    with self.assertRaises(CLIError) as caught:
                        attach(agent(), runner=runner)
                    self.assertNotIn('secret-token', str(caught.exception))


class ShellAttachTests(unittest.TestCase):
    def run_attach(self, argv=None, *, tty=True, api=None, attach=None):
        api = api or RecordingAPI()
        attach = attach or MagicMock(return_value=0)
        stdout, stderr = TerminalInput(tty=tty), StringIO()
        with patch.object(sys, 'stdin', TerminalInput(tty=tty)), \
                redirect_stdout(stdout), redirect_stderr(stderr), \
                patch.object(cli, 'WorkspaceAPI', return_value=api) as factory, \
                patch.object(cli, 'attach_agent', attach, create=True):
            try:
                cli.main(argv or ['attach', 'claude', '--session', 'billing',
                                  '--url', 'http://localhost:8300'])
                code = 0
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue(), api, attach, factory

    def test_attach_parser_accepts_common_options_on_either_side(self):
        for argv in (['--session', 'billing', 'attach', 'ag_a'],
                     ['attach', 'ag_a', '--session', 'billing']):
            with self.subTest(argv=argv), redirect_stderr(StringIO()):
                try:
                    args = cli.build_parser().parse_args(argv)
                except SystemExit:
                    self.fail('attach parser is missing')
                self.assertEqual((args.command, args.agent, args.session), ('attach', 'ag_a', 'billing'))

    def test_attach_resolves_once_without_websocket_or_mutation_and_preserves_exit(self):
        original_import = builtins.__import__
        def no_websocket(name, *args, **kwargs):
            if name.startswith('websockets'):
                raise ImportError('websocket must not be required')
            return original_import(name, *args, **kwargs)
        attach = MagicMock(return_value=7)
        with patch('builtins.__import__', side_effect=no_websocket):
            code, stdout, stderr, api, _, _ = self.run_attach(attach=attach)
        self.assertEqual(code, 7, stderr)
        self.assertEqual(api.calls, [('resolve', 'billing', True)])
        self.assertEqual(attach.call_args.args, (api.selected['agents'][0],))
        self.assertEqual(attach.call_args.kwargs['shell_session'], 'ws_a')
        self.assertEqual(stdout, '')

    def test_attach_validation_precedes_network_and_terminal_takeover(self):
        for argv, tty, hint in [(['attach', 'ag_a'], True, 'requires --session'),
                (['attach', 'ag_a', '--session', 'billing', '--json'], True, '--json'),
                (['attach', 'ag_a', '--session', 'billing'], False, 'requires a terminal')]:
            with self.subTest(argv=argv):
                code, _, stderr, _, attach, factory = self.run_attach(argv, tty=tty)
                self.assertNotEqual(code, 0)
                self.assertIn(hint, stderr)
                factory.assert_not_called()
                attach.assert_not_called()

    def test_attach_windows_rejects_before_api(self):
        with patch('cli_workspaces.os.name', 'nt'):
            code, _, stderr, _, attach, factory = self.run_attach()
        self.assertEqual(code, 1)
        self.assertIn(WINDOWS_ERROR, stderr)
        attach.assert_not_called()
        factory.assert_not_called()

    def test_request_deadline_does_not_limit_foreground_attach(self):
        def attach(*args, **kwargs):
            time.sleep(.04)
            return 0
        code, _, stderr, api, _, _ = self.run_attach(
            ['attach', 'ag_a', '--session', 'billing', '--timeout', '.01',
             '--url', 'http://localhost:8300'], attach=attach)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(api.calls, [('resolve', 'billing', True)])

    def test_request_deadline_still_limits_resolution(self):
        api = RecordingAPI()
        resolve = api.resolve
        def slow_resolve(*args, **kwargs):
            time.sleep(.04)
            return resolve(*args, **kwargs)
        api.resolve = slow_resolve
        code, _, stderr, _, attach, _ = self.run_attach(
            ['attach', 'ag_a', '--session', 'billing', '--timeout', '.01',
             '--url', 'http://localhost:8300'], api=api)
        self.assertEqual(code, 1)
        self.assertIn('timed out', stderr)
        attach.assert_not_called()


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
    def test_provider_flags_reach_spawn_and_resume_with_quotes_and_clear(self):
        for command, target, flags, expected in (
                ('spawn', 'codex', '--model "custom model" --verbose', ['--model', 'custom model', '--verbose']),
                ('resume', 'claude', '', [])):
            api = RecordingAPI()
            words = [command, target, '--session', 'billing', '--provider-flags=' + flags]
            if command == 'spawn':
                words += ['--cwd', '/tmp', '--history-mode', 'none']
            self.runner()(api, self.parse(words))
            self.assertEqual(api.calls[-1][-1]['provider_args'], expected)

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
    def test_sessions_archived_suffix_marks_only_archived_rows(self):
        active = workspace()
        archived = dict(workspace(archived=True), id='ws_old')
        data = {'workspaces': [active, archived]}
        original = json.dumps(data, sort_keys=True)
        text = self.formatter()('sessions', self.result(data))
        labels = [line for line in text.splitlines() if not line.startswith('  ')]
        self.assertEqual(labels, ['billing (ws_a)', 'billing (ws_old) (archived)'])
        self.assertEqual(text.count(' (archived)'), 1)
        self.assertEqual(json.dumps(data, sort_keys=True), original)

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
    def test_json_refusal_names_every_supported_command_for_chat_and_attach(self):
        supported = ('send', 'read', 'channels', 'status', 'sessions', 'new',
                     'spawn', 'resume', 'stop', 'unread', 'retry', 'history', 'archive')
        for argv, command in [(['--json'], 'chat'), (['chat', '--json'], 'chat'),
                              (['attach', 'ag_a', '--session', 'billing', '--json'], 'attach')]:
            with self.subTest(command=command, argv=argv):
                code, stdout, stderr, api_type, runner = self.run_main(argv)
                self.assertEqual(code, 2)
                diagnostic = stderr.split('error: ', 1)[-1]
                self.assertIn(f'--json is not available for {command}', diagnostic)
                for name in supported:
                    self.assertIn(name, diagnostic)
                self.assertEqual(stdout, '')
                api_type.assert_not_called()
                runner.assert_not_called()

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
            "Could not connect to the local yapp server"))
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

    def test_all_shell_commands_share_down_server_error_and_manual_hint(self):
        expected = ("Could not connect to the local yapp server\n"
                    "Start it manually: python run.py\n")
        commands = [
            ["--url", "http://127.0.0.1:1", "send", "--json", "hello"],
            ["--url", "http://127.0.0.1:1", "read", "--json"],
            ["--url", "http://127.0.0.1:1", "status", "--json"],
            ["--url", "http://127.0.0.1:1", "channels", "--json"],
            ["--url", "http://127.0.0.1:1", "sessions", "--json"],
        ]
        opener = MagicMock()
        opener.open.side_effect = OSError("connection refused")
        for argv in commands:
            with self.subTest(command=argv[-2]), \
                    patch.object(cli_api, "_opener", return_value=opener), \
                    patch.object(cli.sys, "stdin", TerminalInput()), \
                    redirect_stdout(stdout := StringIO()), \
                    redirect_stderr(stderr := StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(argv)
                self.assertEqual(caught.exception.code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), expected)

    def test_all_legacy_commands_report_missing_websocket_dependency(self):
        expected = "Install terminal dependencies: python -m pip install -r requirements-cli.txt\n"
        for argv in (["read", "--json"], ["send", "x"]):
            with self.subTest(command=argv[0]), \
                    patch.dict(sys.modules, {"websockets.asyncio.client": None}), \
                    patch.object(cli.sys, "stdin", TerminalInput()), \
                    redirect_stdout(stdout := StringIO()), \
                    redirect_stderr(stderr := StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(argv)
                self.assertEqual(caught.exception.code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), expected)


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
