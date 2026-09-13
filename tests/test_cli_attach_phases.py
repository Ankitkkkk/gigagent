"""Reusable tmux attach phase contracts."""

import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli_workspaces
from cli_api import CLIError
from cli_workspaces import attach_agent, prepare_attach, run_attach


ATTACH_ERROR = 'Could not attach to the agent terminal. Check tmux and retry.'


class TerminalInput:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class PrepareAttachTests(unittest.TestCase):
    def test_preflight_only_probes_exact_target(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        prepared = prepare_attach({'agent_id': 'ag_a', 'registry_name': 'claude-1'}, runner=runner)
        self.assertEqual(prepared.target, 'agentchattr-ag_a')
        runner.assert_called_once_with(['tmux', 'has-session', '-t', '=agentchattr-ag_a'],
                                       timeout=5, capture_output=True)

    def test_preflight_prepares_shell_hint_and_nested_mode(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        agent = {'agent_id': 'ag_a', 'registry_name': 'Claude Worker',
                 'tmux_session': 'server-target'}
        with patch.dict(os.environ, {'TMUX': 'inside'}, clear=True), \
                patch.object(sys, 'stdin', TerminalInput(True)), \
                patch.object(sys, 'stdout', TerminalInput(True)):
            prepared = prepare_attach(agent, runner=runner, shell_session='Billing Room')
        self.assertEqual(prepared.target, 'server-target')
        self.assertEqual(prepared.label, "'Claude Worker'")
        self.assertEqual(
            prepared.hint,
            "python cli.py resume 'Claude Worker' --session 'Billing Room'",
        )
        self.assertTrue(prepared.nested)

    def test_preflight_failure_stops_before_foreground_attach(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, stderr='secret-token'))
        with self.assertRaisesRegex(
                CLIError, r'^not running; resume with /resume claude-1$'):
            attach_agent({'agent_id': 'ag_a', 'registry_name': 'claude-1'}, runner=runner)
        runner.assert_called_once_with(['tmux', 'has-session', '-t', '=agentchattr-ag_a'],
                                       timeout=5, capture_output=True)

    def test_preflight_rejects_non_tty_shell_before_probe(self):
        runner = Mock()
        with patch.object(sys, 'stdin', TerminalInput(False)), \
                patch.object(sys, 'stdout', TerminalInput(True)), \
                self.assertRaisesRegex(CLIError, r'^Attach requires a terminal$'):
            prepare_attach({'agent_id': 'ag_a'}, runner=runner, shell_session='ws_a')
        runner.assert_not_called()

    def test_preflight_runner_errors_use_fixed_message(self):
        for failure in (OSError('secret-token'),
                        subprocess.TimeoutExpired('secret-token', 5)):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaisesRegex(CLIError, '^' + ATTACH_ERROR.replace('.', r'\.') + '$'):
                    prepare_attach({'agent_id': 'ag_a'}, runner=Mock(side_effect=failure))


class RunAttachTests(unittest.TestCase):
    def prepared(self, *, nested=False):
        return cli_workspaces.AttachTarget(
            target='agentchattr-ag_a',
            label='claude-1',
            hint='/resume claude-1',
            nested=nested,
        )

    def test_foreground_attach_uses_only_target_and_preserves_return_code(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 7))
        output = Mock()
        code = run_attach(self.prepared(), runner=runner, output=output)
        self.assertEqual(code, 7)
        runner.assert_called_once_with(['tmux', 'attach', '-t', 'agentchattr-ag_a'])
        output.assert_not_called()

    def test_nested_switch_emits_existing_switch_back_hint_after_success(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        output = Mock()
        code = run_attach(self.prepared(nested=True), runner=runner, output=output)
        self.assertEqual(code, 0)
        runner.assert_called_once_with(
            ['tmux', 'switch-client', '-t', 'agentchattr-ag_a'])
        output.assert_called_once_with('Switch back: tmux switch-client -l')

    def test_nested_switch_failure_preserves_return_code_without_hint(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 9))
        output = Mock()
        code = run_attach(self.prepared(nested=True), runner=runner, output=output)
        self.assertEqual(code, 9)
        runner.assert_called_once_with(
            ['tmux', 'switch-client', '-t', 'agentchattr-ag_a'])
        output.assert_not_called()

    def test_foreground_runner_errors_use_fixed_message(self):
        for failure in (OSError('secret-token'),
                        subprocess.TimeoutExpired('secret-token', 5)):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaisesRegex(CLIError, '^' + ATTACH_ERROR.replace('.', r'\.') + '$'):
                    run_attach(self.prepared(), runner=Mock(side_effect=failure))


class AttachFacadeTests(unittest.TestCase):
    def test_facade_injects_runner_into_both_phases_without_reprobe(self):
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 6),
        ])
        code = attach_agent(
            {'agent_id': 'ag_a', 'registry_name': 'claude-1'}, runner=runner)
        self.assertEqual(code, 6)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(
            runner.call_args_list[0].args,
            (['tmux', 'has-session', '-t', '=agentchattr-ag_a'],),
        )
        self.assertEqual(
            runner.call_args_list[1].args,
            (['tmux', 'attach', '-t', 'agentchattr-ag_a'],),
        )


if __name__ == '__main__':
    unittest.main()
