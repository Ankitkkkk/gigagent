"""Rendered TUI restart control and guarded workflow contracts."""

import unittest
from unittest.mock import AsyncMock, Mock

from cli_api import CLIError
from cli_view_contracts import ActionOutcome
from tests.test_cli_tui_workflows import workflow_harness, workspace


READY = {'instance_id': 'old-instance', 'state': 'ready',
         'previous_instance_id': None, 'restart_supported': True, 'reason': ''}


class RestartWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_visible_and_focusable_at_80x18_plain_without_session(self):
        for plain, size in ((False, (80, 18)), (True, (80, 18)),
                            (False, (120, 28)), (True, (120, 28))):
            with self.subTest(plain=plain, size=size):
                async with workflow_harness(plain=plain, size=size) as ui:
                    await ui.wait_render()
                    self.assertIn('Restart server', ui.screen_text())
                    self.assertTrue(ui.view.focus_named('restart_server'))
                    self.assertIs(ui.application.layout.current_control,
                                  ui.view.restart_server.control)
                    choices = {choice['id']: choice for choice in ui.view.action_choices()}
                    self.assertIn('restart_server', choices)

    async def test_visible_button_accepts_mouse(self):
        async with workflow_harness(plain=True, size=(80, 18)) as ui:
            ui.api.server_status.return_value = dict(
                READY, restart_supported=False, reason='manual restart only')
            await ui.wait_render()
            info = ui.view.restart_server.window.render_info
            await ui.click(info._x_offset + 3, info._y_offset)
            await ui.wait_until(lambda: ui.api.server_status.call_count == 1)
            self.assertIn('manual restart only', ui.state.notices.lines[-1])
            ui.api.restart_server.assert_not_called()

    async def test_default_no_names_url_and_preserves_ui_state(self):
        async with workflow_harness(selected=workspace(), size=(80, 18)) as ui:
            ui.api.server_status.return_value = READY
            await ui.paste('unsent draft')
            await ui.key('Left')
            before = (ui.view.composer.text, ui.view.composer.buffer.cursor_position,
                      ui.view.composer_mode, ui.controller.workspace['id'])
            task = ui.start(ui.workflows.run_action('restart_server'))
            await ui.wait_until(lambda: 'Restart server?' in ui.screen_text())
            self.assertIn('http://127.0.0.1:18300', ui.screen_text())
            await ui.key('Enter')
            self.assertEqual((await task).status, 'cancelled')
            self.assertEqual(before, (ui.view.composer.text, ui.view.composer.buffer.cursor_position,
                                      ui.view.composer_mode, ui.controller.workspace['id']))
            ui.api.restart_server.assert_not_called()

    async def test_confirmed_restart_uses_preflight_instance_once(self):
        async with workflow_harness(selected=workspace(), size=(80, 18)) as ui:
            ui.api.server_status.return_value = READY
            ui.api.restart_server.return_value = dict(READY, instance_id='new-instance')
            await ui.paste('keep this draft')
            await ui.key('Left')
            before = (ui.view.composer.text, ui.view.composer.buffer.cursor_position,
                      ui.view.composer_mode, ui.controller.workspace['id'])
            task = ui.start(ui.workflows.run_action('restart_server'))
            await ui.wait_until(lambda: 'Restart server?' in ui.screen_text())
            await ui._send('y')
            self.assertEqual((await task).status, 'completed')
            ui.api.restart_server.assert_called_once_with('old-instance')
            self.assertEqual(before, (ui.view.composer.text, ui.view.composer.buffer.cursor_position,
                                      ui.view.composer_mode, ui.controller.workspace['id']))
            self.assertIn('Server restarted; chat reconnects automatically.', ui.state.notices.lines)

    async def test_nonready_preflight_and_restart_error_remain_visible(self):
        for response in (dict(READY, state='starting'), dict(READY, state='restarting')):
            with self.subTest(state=response['state']):
                async with workflow_harness() as ui:
                    ui.api.server_status.return_value = response
                    result = await ui.workflows.run_action('restart_server')
                    self.assertEqual(result.status, 'failed')
                    self.assertIn(response['state'], ui.state.notices.lines[-1])
                    ui.api.restart_server.assert_not_called()
        async with workflow_harness() as ui:
            ui.api.server_status.return_value = READY
            ui.api.restart_server.side_effect = CLIError('restart remains uncertain')
            task = ui.start(ui.workflows.run_action('restart_server'))
            await ui.wait_until(lambda: 'Restart server?' in ui.screen_text())
            await ui._send('y')
            self.assertEqual((await task).status, 'failed')
            self.assertIn('restart remains uncertain', ui.state.notices.lines[-1])

    async def test_unsupported_and_preflight_error_never_confirm_or_mutate(self):
        for response in (dict(READY, restart_supported=False, reason='Use run.py'),
                         CLIError('Restart run.py manually first')):
            with self.subTest(response=response):
                async with workflow_harness() as ui:
                    ui.api.server_status.side_effect = response if isinstance(response, Exception) else None
                    ui.api.server_status.return_value = response if isinstance(response, dict) else None
                    result = await ui.workflows.run_action('restart_server')
                    self.assertEqual(result.status, 'failed')
                    self.assertIsNone(ui.dialogs.future)
                    ui.api.restart_server.assert_not_called()

    async def test_double_activation_owns_single_preflight_and_mutation(self):
        async with workflow_harness() as ui:
            entered = __import__('asyncio').Event()
            release = __import__('asyncio').Event()
            async def restart_action(action, payload):
                entered.set()
                await release.wait()
                return ActionOutcome('completed')
            ui.api.server_status.return_value = READY
            ui.controller.execute_action = AsyncMock(side_effect=restart_action)
            first = ui.start(ui.workflows.run_action('restart_server'))
            await ui.wait_until(lambda: 'Restart server?' in ui.screen_text())
            await ui._send('y')
            await entered.wait()
            second = await ui.workflows.run_action('restart_server')
            self.assertEqual(second.status, 'cancelled')
            release.set()
            self.assertEqual((await first).status, 'completed')
            ui.api.server_status.assert_called_once_with()
            ui.controller.execute_action.assert_awaited_once()

    async def test_selection_change_after_preflight_cancels_before_confirmation(self):
        async with workflow_harness(selected=workspace(), rows=[workspace('ws_other')]) as ui:
            import threading
            entered, release = threading.Event(), threading.Event()
            def status():
                entered.set()
                release.wait(2)
                return READY
            ui.api.server_status.side_effect = status
            task = ui.start(ui.workflows.run_action('restart_server'))
            self.assertTrue(await __import__('asyncio').to_thread(entered.wait, 1))
            ui.controller._select(ui.records['ws_other'])
            release.set()
            self.assertEqual((await task).status, 'cancelled')
            self.assertIsNone(ui.dialogs.future)
            ui.api.restart_server.assert_not_called()
