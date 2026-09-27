"""`yapp update` command."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import cli
import cli_update
from cli_api import CLIError

AVAILABLE = {'state': 'update_available', 'current': '0.5.0', 'latest': '0.6.0', 'tag': 'v0.6.0',
             'url': 'https://github.com/Ankitkkkk/yapp/releases/tag/v0.6.0',
             'archive_url': 'https://github.com/Ankitkkkk/yapp/archive/refs/tags/v0.6.0.zip', 'error': ''}
CONFIG = {'server': {'data_dir': '/tmp/yapp-test-data', 'port': 8300}}


def args(**kwargs):
    base = dict(check=False, yes=False, json=False, url=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


class UpdateCommandTests(unittest.TestCase):
    def run_update(self, arguments, *, result=AVAILABLE, method='installer', answer='y', tty=True,
                   applied=None, api=None):
        self.lines = []
        self.apply = Mock(return_value=applied or {'ok': True, 'state': 'installed',
                                                   'version': '0.6.0', 'message': 'Installed yapp 0.6.0.'})
        self.check = Mock(return_value=result)
        self.api = api or Mock()
        if api is None:
            self.api.server_status.side_effect = CLIError('Could not connect to the local yapp server')
        return cli_update.run_update(
            arguments, CONFIG, output=self.lines.append, ask=lambda prompt: answer,
            stdin_tty=lambda: tty, check=self.check, apply=self.apply,
            install_method=lambda: method, api_factory=lambda url: self.api)

    def test_check_exit_codes(self):
        self.assertEqual(self.run_update(args(check=True)), 10)
        self.assertEqual(self.run_update(args(check=True), result=dict(AVAILABLE, state='current')), 0)
        self.assertEqual(self.run_update(args(check=True), result=dict(AVAILABLE, state='unknown',
                                                                      error='Could not reach GitHub')), 1)
        self.apply.assert_not_called()
        self.check.assert_called_with(data_dir=cli_update.updates.data_dir_from_config(CONFIG),
                                      config=CONFIG, force=True, explicit=True)

    def test_check_json(self):
        self.run_update(args(check=True, json=True))
        self.assertEqual(json.loads(self.lines[-1])['state'], 'update_available')

    def test_prompt_declined_does_nothing(self):
        self.assertEqual(self.run_update(args(), answer='n'), 0)
        self.apply.assert_not_called()

    def test_non_tty_requires_yes(self):
        self.assertEqual(self.run_update(args(), tty=False), 1)
        self.assertIn('--yes', self.lines[-1])
        self.apply.assert_not_called()

    def test_yes_applies_and_restarts_ready_local_server(self):
        api = Mock()
        api.server_status.return_value = {'instance_id': 'i1', 'state': 'ready',
                                          'restart_supported': True, 'reason': ''}
        self.assertEqual(self.run_update(args(yes=True), api=api), 0)
        self.apply.assert_called_once()
        api.restart_server.assert_called_once_with('i1')
        self.assertTrue(any('Installed yapp 0.6.0.' in line for line in self.lines))

    def test_no_server_running_is_fine(self):
        self.assertEqual(self.run_update(args(yes=True)), 0)
        self.assertTrue(any('No local server' in line for line in self.lines))

    def test_unsupported_method_prints_instructions(self):
        self.assertEqual(self.run_update(args(yes=True), method='checkout'), 1)
        self.assertIn('git pull', self.lines[-1])
        self.apply.assert_not_called()

    def test_apply_failure(self):
        code = self.run_update(args(yes=True), applied={'ok': False, 'state': 'failed', 'version': '',
                                                        'message': 'Install failed: boom.'})
        self.assertEqual(code, 1)
        self.assertIn('boom', self.lines[-1])

    def test_already_current(self):
        self.assertEqual(self.run_update(args(), result=dict(AVAILABLE, state='current', current='0.6.0')), 0)
        self.assertIn('up to date', self.lines[-1])

    def test_parser_accepts_update_flags(self):
        parsed = cli.build_parser().parse_args(['update', '--check', '--json'])
        self.assertEqual((parsed.command, parsed.check, parsed.json), ('update', True, True))
        parsed = cli.build_parser().parse_args(['update', '--yes'])
        self.assertTrue(parsed.yes)


if __name__ == '__main__':
    unittest.main()
