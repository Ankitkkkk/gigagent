"""Terminal contracts for saved profiles and session orchestrators."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import cli
from cli import ChatClient
from cli_workspace_chat import WorkspaceChatController, _agent_line, _parse_command
from cli_workspaces import WorkspaceAPI, run_workspace_command
from cli_view_contracts import ActionOutcome
from cli_tui_dialogs import ModalResult
from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


class RecordingAPI:
    def __init__(self):
        self.calls = []
        self.workspace = {'id': 'ws_one', 'name': 'One', 'channel': 'general', 'agents': []}

    def create(self, name='', *, orchestrator=None):
        self.calls.append(('create', name, orchestrator))
        return self.workspace

    def resolve(self, selector, include_archived=True):
        self.calls.append(('resolve', selector, include_archived))
        return self.workspace

    def action(self, ws_id, action, agent_id=None, body=None):
        self.calls.append(('action', ws_id, action, agent_id, body))
        return {'agent_id': 'ag_one', 'registry_name': 'codex-1',
                'provider': 'codex', 'cwd': '/tmp', 'last_state': 'starting'}


class ShellContractTests(unittest.TestCase):
    def parse(self, words):
        return cli.build_parser().parse_args(words)

    def test_new_can_create_resident_orchestrator(self):
        api = RecordingAPI()
        args = self.parse(['new', 'One', '--orchestrator-provider', 'codex',
                           '--cwd', '/tmp', '--provider-flags=--model fast'])
        run_workspace_command(api, args)
        self.assertEqual(api.calls, [('create', 'One', {
            'provider': 'codex', 'cwd': '/tmp', 'provider_args': ['--model', 'fast']})])

    def test_spawn_sends_profile_presets(self):
        api = RecordingAPI()
        args = self.parse(['spawn', 'codex', '--session', 'One', '--cwd', '/tmp',
                           '--role', 'tester', '--personality', 'meticulous'])
        with patch('cli_workspaces.os', SimpleNamespace(name='posix')):
            run_workspace_command(api, args)
        self.assertEqual(api.calls[-1][-1], {
            'provider': 'codex', 'cwd': '/tmp', 'history_mode': 'literal',
            'name': None, 'role': 'tester', 'personality': 'meticulous'})

    def test_slash_spawn_accepts_profile_presets(self):
        args = _parse_command('/spawn', ['codex', '--role', 'planner',
                                         '--personality', 'concise'])
        self.assertEqual((args.role, args.personality), ('planner', 'concise'))


class APIContractTests(unittest.TestCase):
    def test_create_omits_orchestrator_for_legacy_callers(self):
        api = WorkspaceAPI.__new__(WorkspaceAPI)
        api.request = Mock(return_value={})
        api.create('Legacy')
        api.request.assert_called_once_with('POST', '/api/workspaces', {'name': 'Legacy'})

    def test_configure_orchestrator_posts_configuration(self):
        api = WorkspaceAPI.__new__(WorkspaceAPI)
        api.request = Mock(side_effect=[{'session_orchestrator': 1}, {}])
        api.configure_orchestrator('ws/a', {'provider': 'kilo', 'cwd': '/tmp'})
        self.assertEqual(api.request.call_args_list, [
            unittest.mock.call('GET', '/api/terminal-capabilities'),
            unittest.mock.call('POST', '/api/workspaces/ws%2Fa/orchestrator',
                               {'provider': 'kilo', 'cwd': '/tmp'})])

    def test_profile_spawn_checks_capability_before_mutation(self):
        api = WorkspaceAPI.__new__(WorkspaceAPI)
        api.request = Mock(side_effect=[{'agent_profiles': 1}, {'agent_id': 'ag_one'}])
        api.action('ws_one', 'spawn', body={'provider': 'codex', 'cwd': '/tmp',
                   'role': 'tester', 'personality': 'concise'})
        self.assertEqual(api.request.call_args_list[0],
                         unittest.mock.call('GET', '/api/terminal-capabilities'))

    def test_missing_capability_refuses_before_post(self):
        api = WorkspaceAPI.__new__(WorkspaceAPI)
        api.request = Mock(return_value={})
        with self.assertRaisesRegex(Exception, 'Restart'):
            api.create('New', orchestrator={'provider': 'codex', 'cwd': '/tmp'})
        api.request.assert_called_once_with('GET', '/api/terminal-capabilities')


class PresentationContractTests(unittest.TestCase):
    def test_agent_line_shows_profile_and_orchestrator_badges(self):
        row = {'agent_id': 'ag_one', 'registry_name': 'codex-1', 'last_state': 'running',
               'native_session_id': 'native', 'cwd': '/tmp', 'kind': 'orchestrator',
               'profile': {'role': 'planner', 'personality': 'concise'}}
        line = _agent_line(row)
        self.assertIn('orchestrator', line)
        self.assertIn('planner', line)
        self.assertIn('concise', line)


class ControllerContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_session_passes_orchestrator_and_configure_action(self):
        api = Mock()
        api.create.return_value = {'id': 'ws_new'}
        api.configure_orchestrator.return_value = {'id': 'ws_one'}
        client = ChatClient('http://127.0.0.1:18300')
        controller = WorkspaceChatController(client, api, providers=['codex'])
        created = await controller.execute_action('create_session', {
            'name': 'New', 'orchestrator': {'provider': 'codex', 'cwd': '/tmp'}})
        self.assertEqual(created.status, 'completed')
        api.create.assert_called_once_with(
            'New', orchestrator={'provider': 'codex', 'cwd': '/tmp'})

        controller._select({'id': 'ws_one', 'name': 'One', 'channel': 'general', 'agents': []})
        configured = await controller.execute_action('configure_orchestrator', {
            'provider': 'codex', 'cwd': '/tmp'})
        self.assertEqual(configured.status, 'completed')
        api.configure_orchestrator.assert_called_once_with(
            'ws_one', {'provider': 'codex', 'cwd': '/tmp'})

    async def test_partial_create_surfaces_orchestrator_error_once(self):
        output = []
        api = Mock()
        api.create.return_value = {'id': 'ws_new', 'orchestration_error': 'provider refused'}
        controller = WorkspaceChatController(
            ChatClient('http://127.0.0.1:18300', output=output.append), api)
        result = await controller.execute_action('create_session', {
            'name': 'New', 'orchestrator': {'provider': 'codex', 'cwd': '/tmp'}})
        self.assertEqual(result.status, 'completed')
        self.assertEqual(output, ['Session saved, but orchestrator failed: provider refused'])


class TuiWorkflowContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_uses_staged_orchestrator_form(self):
        async with workflow_harness() as ui:
            ui.workflows._dialog = AsyncMock(side_effect=[
                ModalResult({'name': 'New'}),
                ModalResult({'provider': 'kilo', 'cwd': '/tmp', 'provider_flags': '--fast'}),
            ])
            ui.controller.execute_action = AsyncMock(return_value=ActionOutcome('cancelled'))
            await ui.workflows.new_session()
            calls = ui.workflows._dialog.await_args_list
            self.assertEqual([item.args[1] for item in calls],
                             ['New session · 1 of 2', 'New session · 2 of 2'])
            ui.controller.execute_action.assert_awaited_once_with('create_session', {
                'name': 'New', 'orchestrator': {
                    'provider': 'kilo', 'cwd': '/tmp', 'provider_args': ['--fast']}})

    async def test_add_agent_stages_profile_selection(self):
        async with workflow_harness(selected=workspace()) as ui:
            ui.workflows._dialog = AsyncMock(side_effect=[
                ModalResult({'provider': 'codex', 'cwd': '/tmp', 'name': '',
                             'history_mode': 'literal', 'provider_flags': ''}),
                ModalResult({'role': 'debugger', 'personality': 'meticulous'}),
            ])
            ui.controller.execute_action = AsyncMock(return_value=ActionOutcome('cancelled'))
            await ui.workflows.agent_form('spawn')
            calls = ui.workflows._dialog.await_args_list
            self.assertEqual([item.args[1] for item in calls],
                             ['New agent · 1 of 2', 'New agent · 2 of 2'])
            ui.controller.execute_action.assert_awaited_once_with('spawn', {
                'provider': 'codex', 'cwd': '/tmp', 'name': None, 'history_mode': 'literal',
                'role': 'debugger', 'personality': 'meticulous'})

    async def test_resume_shows_locked_profile(self):
        saved = agent()
        saved['profile'] = {'role': 'tester', 'personality': 'supportive'}
        async with workflow_harness(selected=workspace(agents=[saved])) as ui:
            ui.workflows._dialog = AsyncMock(return_value=ModalResult(cancelled=True))
            await ui.workflows.agent_form('resume', 'ag_one')
            description = ui.workflows._dialog.await_args.kwargs['description']
            self.assertIn('Locked profile: tester · supportive', description)

    async def test_legacy_resume_does_not_invent_profile(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.workflows._dialog = AsyncMock(return_value=ModalResult(cancelled=True))
            await ui.workflows.agent_form('resume', 'ag_one')
            description = ui.workflows._dialog.await_args.kwargs['description']
            self.assertIn('Legacy agent — no saved profile', description)

    async def test_profile_choice_survives_backend_retry(self):
        async with workflow_harness(selected=workspace()) as ui:
            basics = {'provider': 'codex', 'cwd': '/tmp', 'name': '',
                      'history_mode': 'literal', 'provider_flags': ''}
            ui.workflows._dialog = AsyncMock(side_effect=[
                ModalResult(basics), ModalResult({'role': 'debugger', 'personality': 'supportive'}),
                ModalResult(basics), ModalResult(cancelled=True),
            ])
            ui.controller.execute_action = AsyncMock(return_value=ActionOutcome('failed', 'refused'))
            await ui.workflows.agent_form('spawn')
            retry_fields = ui.workflows._dialog.await_args_list[3].args[2]
            self.assertEqual({field.name: field.default for field in retry_fields},
                             {'role': 'debugger', 'personality': 'supportive'})

    async def test_new_session_details_fit_minimum_terminal(self):
        async with workflow_harness(size=(80, 18)) as ui:
            task = ui.start(ui.workflows.new_session())
            await ui.wait_until(lambda: 'New session · 1 of 2' in ui.screen_text())
            await ui.key('Enter')
            await ui.wait_until(lambda: 'New session · 2 of 2' in ui.screen_text())
            screen = ui.screen_text()
            for text in ('Orchestrator provider:', 'Working directory:', 'Provider flags:',
                         'Create session', 'Cancel'):
                self.assertIn(text, screen)
            self.assertNotIn('Window too small', screen)
            await ui.key('Escape')
            self.assertEqual((await task).status, 'cancelled')

    async def test_existing_session_can_enable_orchestrator(self):
        async with workflow_harness(selected=workspace()) as ui:
            choices = {choice['id']: choice for choice in ui.view.action_choices()}
            self.assertIn('enable_orchestrator', choices)
            ui.workflows._dialog = AsyncMock(return_value=ModalResult({
                'provider': 'codex', 'cwd': '/tmp', 'provider_flags': ''}))
            ui.controller.execute_action = AsyncMock(return_value=ActionOutcome('completed'))
            await ui.workflows.run_action('enable_orchestrator')
            ui.controller.execute_action.assert_awaited_once_with(
                'configure_orchestrator', {'provider': 'codex', 'cwd': '/tmp'})

    async def test_compact_rows_show_role_and_manager_kind(self):
        worker = agent('ag_worker')
        worker['profile'] = {'role': 'tester', 'personality': 'concise'}
        manager = agent('ag_manager')
        manager['kind'] = 'orchestrator'
        async with workflow_harness(selected=workspace(agents=[worker, manager]), size=(80, 18)) as ui:
            text = ''.join(part[1] for part in ui.view._agent_fragments())
            self.assertIn('tester', text)
            self.assertIn('orchestrator', text)
