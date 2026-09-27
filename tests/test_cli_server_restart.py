"""Client restart fencing and readiness polling contracts."""

import unittest
from unittest.mock import Mock, call, patch

from cli_api import CLIError
from cli_workspaces import WorkspaceAPI


class ServerRestartClientTests(unittest.TestCase):
    def api(self):
        api = WorkspaceAPI.__new__(WorkspaceAPI)
        api.url = 'http://127.0.0.1:8300'
        api.timeout = 15
        api._token = 'old-token'
        return api

    def test_old_server_preflight_gives_manual_upgrade_guidance(self):
        api = self.api()
        api.request = Mock(side_effect=CLIError('Not Found', 404))
        with self.assertRaisesRegex(CLIError, 'Restart run.py manually.*upgrade'):
            api.server_status()

    def test_restart_posts_once_then_uses_fresh_tokens_until_new_ready_instance(self):
        api = self.api()
        responses = [{'instance_id': 'old', 'state': 'restarting'}, CLIError('offline'),
                    {'instance_id': 'old', 'state': 'restarting',
                     'previous_instance_id': None, 'restart_supported': True, 'reason': ''},
                    {'instance_id': 'unrelated', 'state': 'ready',
                     'previous_instance_id': 'other', 'restart_supported': True, 'reason': ''},
                    {'instance_id': 'new', 'state': 'starting',
                     'previous_instance_id': 'old', 'restart_supported': True, 'reason': ''},
                    {'instance_id': 'new', 'state': 'ready',
                     'previous_instance_id': 'old', 'restart_supported': True, 'reason': ''}]
        with patch('cli_workspaces.cli_api.fetch_session_token',
                   side_effect=['t1', 't2', 't3', 't4', 't5']) as tokens, \
                patch('cli_workspaces.cli_api.request_json', side_effect=responses) as request, \
                patch('cli_workspaces.time.sleep'), \
                patch('cli_workspaces.cli_api.monotonic', side_effect=[i / 10 for i in range(20)]):
            result = api.restart_server('old', timeout=5)
        self.assertEqual(tokens.call_count, 5)
        self.assertEqual([item.args[2] for item in request.call_args_list],
                         ['POST', 'GET', 'GET', 'GET', 'GET', 'GET'])
        self.assertEqual(result['instance_id'], 'new')

    def test_lost_post_ack_observes_matching_successor_without_retry(self):
        api = self.api()
        successor = {'instance_id': 'new', 'state': 'ready',
                     'previous_instance_id': 'old', 'restart_supported': True, 'reason': ''}
        with patch('cli_workspaces.cli_api.request_json',
                   side_effect=[CLIError('connection closed'), successor]) as request, \
                patch('cli_workspaces.cli_api.fetch_session_token', return_value='fresh'), \
                patch('cli_workspaces.cli_api.monotonic', side_effect=[0, .1, .2, .3, .4]):
            self.assertEqual(api.restart_server('old', timeout=5), successor)
        self.assertEqual([item.args[2] for item in request.call_args_list], ['POST', 'GET'])

    def test_uncertain_post_is_never_retried_and_timeout_names_recovery(self):
        api = self.api()
        with patch('cli_workspaces.cli_api.request_json', side_effect=CLIError('Could not connect')) as request, \
                patch('cli_workspaces.cli_api.fetch_session_token', side_effect=OSError), \
                patch('cli_workspaces.time.sleep'), \
                patch('cli_workspaces.cli_api.monotonic', side_effect=[0, .1, .2, 1.1, 1.2]):
            with self.assertRaisesRegex(CLIError, 'outcome is uncertain.*confirming it stopped'):
                api.restart_server('old', timeout=1)
        self.assertEqual(sum(item.args[2] == 'POST' for item in request.call_args_list), 1)

    def test_stale_or_unsupported_post_errors_do_not_poll(self):
        for status in (409, 503):
            with self.subTest(status=status):
                api = self.api()
                with patch('cli_workspaces.cli_api.request_json', side_effect=CLIError('refused', status)) as request, \
                        patch('cli_workspaces.cli_api.fetch_session_token') as tokens:
                    with self.assertRaisesRegex(CLIError, 'refused'):
                        api.restart_server('old')
                tokens.assert_not_called()
                request.assert_called_once()
