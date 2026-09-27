"""The branded entry point retains CLI parsing without launching agents."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class YappEntryTests(unittest.TestCase):
    def test_branded_help_and_subcommand_help_work_outside_checkout(self):
        entry = Path(__file__).resolve().parents[1] / 'yapp.py'
        with tempfile.TemporaryDirectory(prefix='yapp help ') as directory:
            for args in (['--help'], ['spawn', '--help']):
                result = subprocess.run([sys.executable, str(entry), *args],
                                        cwd=directory, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage: yapp', result.stdout)
                self.assertIn('--session', result.stdout)
                if args[0] == 'spawn':
                    self.assertIn('--provider-flags', result.stdout)

    def test_goon_command_name_is_shown_in_help(self):
        entry = Path(__file__).resolve().parents[1] / 'yapp.py'
        with tempfile.TemporaryDirectory() as directory:
            goon = Path(directory) / 'goon'
            goon.symlink_to(entry)
            result = subprocess.run([sys.executable, str(goon), '--help'],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage: goon', result.stdout)

    def test_nonterminal_start_explains_shell_commands_without_starting_server(self):
        entry = Path(__file__).resolve().parents[1] / 'yapp.py'
        result = subprocess.run([sys.executable, str(entry)], input='',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Interactive chat requires a terminal', result.stderr)
