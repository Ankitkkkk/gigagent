"""Stop-all scope, confirmation, partial failure and operation ownership."""
import asyncio
import copy
import threading
import unittest
from unittest.mock import patch

from cli_api import CLIError
from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


def stop_api(ui, failures=()):
    calls = []
    original = ui.api.action.side_effect

    def action(ws_id, operation, agent_id=None, **kwargs):
        if operation != 'stop_verified':
            return original(ws_id, operation, agent_id, **kwargs)
        calls.append((ws_id, agent_id))
        if agent_id in failures:
            raise CLIError('stop refused', 409)
        value = next(a for a in ui.records[ws_id]['agents'] if a['agent_id'] == agent_id)
        value['last_state'] = 'exited'
        return dict(copy.deepcopy(value), stop_confirmed=True)

    ui.api.action.side_effect = action
    return calls


class StopAllTests(unittest.IsolatedAsyncioTestCase):
    async def confirm(self, ui, answer='y'):
        await ui.wait_until(lambda: ui.dialogs.future is not None
                           and 'Stop all agents?' in ui.screen_text())
        await ui._send(answer)

    async def test_palette_cancel_defaults_to_no_and_preserves_draft(self):
        async with workflow_harness(selected=workspace(agents=[agent(state='running')])) as ui:
            calls = stop_api(ui)
            await ui.paste('unsent draft')
            await ui.key('Left')
            cursor = ui.view.composer.buffer.cursor_position
            await ui.key('F4')
            await ui.type_text('Stop all agents')
            await ui.key('Enter')
            await ui.wait_until(lambda: 'Stop all agents?' in ui.screen_text())
            self.assertIn('1 agent', ui.screen_text())
            await ui.key('Enter')  # Default No.
            await ui.wait_until(lambda: ui.dialogs.future is None)
            self.assertEqual(calls, [])
            self.assertEqual(ui.view.composer.text, 'unsent draft')
            self.assertEqual(ui.view.composer.buffer.cursor_position, cursor)
            self.assertEqual(ui.view.composer_mode, 'INSERT')

    async def test_all_sessions_including_archived_and_only_confirmed_targets(self):
        selected = workspace(agents=[agent(state='running'), agent('ag_done')])
        other = workspace('ws_other', agents=[agent('ag_start', state='starting')])
        archived = workspace('ws_archived', archived=True, agents=[agent('ag_old', state='running')])
        async with workflow_harness(selected=selected, rows=[other, archived], size=(80, 18)) as ui:
            calls = stop_api(ui)
            await ui.paste('keep draft')
            task = ui.start(ui.workflows.run_action('stop_all'))
            await ui.wait_until(lambda: 'Stop all agents?' in ui.screen_text())
            self.assertIn('4 agents', ui.screen_text())
            self.assertIn('3 sessions', ui.screen_text())
            # Agents launched after the displayed snapshot are not confirmed.
            ui.records['ws_other']['agents'].append(agent('ag_new', state='running'))
            await ui._send('y')
            result = await task
            self.assertEqual(result.status, 'completed')
            self.assertCountEqual(calls, [('ws_one', 'ag_one'), ('ws_one', 'ag_done'), ('ws_other', 'ag_start'),
                                          ('ws_archived', 'ag_old')])
            self.assertEqual(ui.records['ws_other']['agents'][1]['last_state'], 'running')
            self.assertEqual(ui.controller.workspace['id'], 'ws_one')
            self.assertEqual(ui.controller.workspace['agents'][0]['last_state'], 'exited')
            self.assertEqual(ui.controller.workspace['agents'][0]['native_session_id'], 'native')
            self.assertEqual(len(ui.controller.workspace['agents']), 2)
            self.assertEqual(ui.view.composer.text, 'keep draft')
            self.assertEqual(ui.view.composer_mode, 'INSERT')

    async def test_orchestrators_stop_before_workers(self):
        manager = agent('ag_manager', state='running')
        manager['kind'] = 'orchestrator'
        selected = workspace(agents=[agent('ag_worker', state='running'), manager])
        async with workflow_harness(selected=selected) as ui:
            calls = stop_api(ui)
            task = ui.start(ui.workflows.run_action('stop_all'))
            await self.confirm(ui)
            self.assertEqual((await task).status, 'completed')
            self.assertEqual(calls, [('ws_one', 'ag_manager'), ('ws_one', 'ag_worker')])

    async def test_global_action_works_without_selected_session(self):
        for plain in (False, True):
            with self.subTest(plain=plain):
                async with workflow_harness(rows=[workspace(agents=[agent(state='running')])],
                                            plain=plain) as ui:
                    calls = stop_api(ui)
                    choices = {c['id']: c for c in ui.view.action_choices()}
                    self.assertIn('stop_all', choices)
                    self.assertFalse(choices['stop_all']['disabled_reason'])
                    task = ui.start(ui.workflows.run_action('stop_all'))
                    await self.confirm(ui)
                    self.assertEqual((await task).status, 'completed')
                    self.assertEqual(calls, [('ws_one', 'ag_one')])
                    self.assertIsNone(ui.controller.workspace)

    async def test_no_saved_agents_is_a_noop(self):
        async with workflow_harness(selected=workspace()) as ui:
            calls = stop_api(ui)
            result = await ui.workflows.run_action('stop_all')
            self.assertEqual(result.status, 'completed')
            self.assertIn('No saved agents', result.message)
            self.assertIsNone(ui.dialogs.future)
            self.assertEqual(calls, [])

    async def test_partial_failure_continues_and_identifies_unconfirmed_agent(self):
        async with workflow_harness(selected=workspace(agents=[agent(state='running')]),
                rows=[workspace('ws_other', agents=[agent('ag_two', state='running')])]) as ui:
            calls = stop_api(ui, failures={'ag_one'})
            task = ui.start(ui.workflows.run_action('stop_all'))
            await self.confirm(ui)
            result = await task
            self.assertEqual(result.status, 'failed')
            self.assertIn('1 of 2', result.message)
            self.assertIn('ws_one/ag_one', result.message)
            self.assertIn('stop refused', result.message)
            self.assertEqual(len(calls), 2)
            self.assertEqual(ui.records['ws_other']['agents'][0]['last_state'], 'exited')
            self.assertEqual(ui.controller.workspace['agents'][0]['last_state'], 'running')

    async def test_selection_change_cancels_confirmation(self):
        async with workflow_harness(selected=workspace(agents=[agent(state='running')])) as ui:
            calls = stop_api(ui)
            task = ui.start(ui.workflows.run_action('stop_all'))
            await ui.wait_until(lambda: 'Stop all agents?' in ui.screen_text())
            ui.controller._select(None)
            await ui._send('y')
            self.assertEqual((await task).status, 'cancelled')
            self.assertEqual(calls, [])

    async def test_invalid_or_failed_inventory_never_stops_a_partial_list(self):
        for response in (CLIError('offline'), {}, {'workspaces': [workspace(), {'id': 'bad'}]}):
            with self.subTest(response=response):
                async with workflow_harness() as ui:
                    ui.api.list.side_effect = response if isinstance(response, Exception) else lambda **_: response
                    result = await ui.workflows.run_action('stop_all')
                    self.assertEqual(result.status, 'failed')
                    self.assertIsNone(ui.dialogs.future)
                    ui.api.action.assert_not_called()

    async def test_inflight_bulk_stop_is_owned_and_not_repeated(self):
        selected = workspace(agents=[agent(state='running'), agent('ag_two', state='running')])
        async with workflow_harness(selected=selected) as ui:
            calls = stop_api(ui)
            action = ui.api.action.side_effect
            entered, release = threading.Event(), threading.Event()
            def delayed(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('stop barrier timed out')
                return action(*args, **kwargs)
            ui.api.action.side_effect = delayed
            payload = {'confirmed': True, 'targets': (('ws_one', 'ag_one', None), ('ws_one', 'ag_two', None))}
            task = ui.start(ui.controller.execute_action('stop_all', payload))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                duplicate = await ui.controller.execute_action('stop_all', payload)
                self.assertEqual(duplicate.status, 'cancelled')
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await ui.controller.wait_pending()
                self.assertEqual(calls, [('ws_one', 'ag_one'), ('ws_one', 'ag_two')])
                self.assertTrue(all(a['last_state'] == 'exited' for a in ui.records['ws_one']['agents']))
            finally:
                release.set()

    async def test_decline_and_invalid_targets_do_not_mutate(self):
        async with workflow_harness() as ui:
            for targets in ('all', [('ws_one',)], [('ws_one', '', None)], [('ws_one', 'ag_one', 123)]):
                result = await ui.controller.execute_action('stop_all', {'confirmed': True, 'targets': targets})
                self.assertEqual(result.status, 'failed')
            result = await ui.controller.execute_action('stop_all', {'confirmed': False,
                                                                 'targets': (('ws_one', 'ag_one', None),)})
            self.assertEqual(result.status, 'cancelled')
            ui.api.action.assert_not_called()

    async def test_unconfirmed_stop_response_is_reported(self):
        async with workflow_harness(selected=workspace(agents=[agent(state='running')])) as ui:
            ui.api.action.return_value = agent(state='running')
            ui.api.action.side_effect = None
            result = await ui.controller.execute_action('stop_all', {'confirmed': True,
                                                                'targets': (('ws_one', 'ag_one', None),)})
            self.assertEqual(result.status, 'failed')
            self.assertIn('not confirm', result.message)
            ui.api.action.assert_called_once_with('ws_one', 'stop_verified', 'ag_one', body={'expected_nonce': None})

    async def test_windows_disables_global_stop(self):
        async with workflow_harness() as ui:
            with patch('cli_tui_view.sys.platform', 'win32'):
                choices = {c['id']: c for c in ui.view.action_choices()}
                self.assertIn('tmux', choices['stop_all']['disabled_reason'])
