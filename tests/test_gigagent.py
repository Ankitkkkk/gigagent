"""The branded entry point retains CLI parsing without launching agents."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class GigagentEntryTests(unittest.TestCase):
    def test_branded_help_and_subcommand_help_work_outside_checkout(self):
        entry = Path(__file__).resolve().parents[1] / 'gigagent.py'
        with tempfile.TemporaryDirectory(prefix='gigagent help ') as directory:
            for args in (['--help'], ['spawn', '--help']):
                result = subprocess.run([sys.executable, str(entry), *args],
                                        cwd=directory, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage: gigagent', result.stdout)
                self.assertIn('--session', result.stdout)
                if args[0] == 'spawn':
                    self.assertIn('--provider-flags', result.stdout)

    def test_nonterminal_start_explains_shell_commands_without_starting_server(self):
        entry = Path(__file__).resolve().parents[1] / 'gigagent.py'
        result = subprocess.run([sys.executable, str(entry)], input='',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Interactive chat requires a terminal', result.stderr)
