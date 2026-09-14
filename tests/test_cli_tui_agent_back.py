"""Agent chooser stays reachable from actions through F3 and Back."""
import unittest

from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


class AgentBackTests(unittest.IsolatedAsyncioTestCase):
    async def test_f3_always_lists_agents_and_escape_unwinds_one_level(self):
        for rows in ([agent()], [agent(), agent('ag_two')]):
            async with workflow_harness(selected=workspace(agents=rows)) as ui:
                ui.state.selected_agent_id = 'ag_one'
                ui.view.composer.text = 'keep draft'
                await ui.key('F3')
                await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
                await ui.key('Enter')
                await ui.wait_until(lambda: 'Agent actions: ag_one' in ui.screen_text())
                await ui.key('Escape')
                await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
                await ui.key('Escape')
                await ui.wait_until(lambda: ui.dialogs.future is None)
                self.assertEqual(ui.focused_control, 'composer')
                self.assertEqual(ui.view.composer.text, 'keep draft')
                await ui.key('F3')
                await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())

    async def test_f3_in_actions_discards_filter_and_allows_another_agent(self):
        async with workflow_harness(selected=workspace(agents=[agent(), agent('ag_two')])) as ui:
            ui.state.selected_agent_id = 'ag_one'
            ui.view.focus_named('agents')
            await ui.key('Enter')
            await ui.wait_until(lambda: 'Agent actions: ag_one' in ui.screen_text())
            await ui.type_text('stop')
            await ui.key('F3')
            await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
            await ui.type_text('ag_two')
            await ui.key('Enter')
            await ui.wait_until(lambda: 'Agent actions: ag_two' in ui.screen_text())
            y, row = next((y, row) for y, row in enumerate(ui.rows) if '<   Back   >' in row)
            await ui.click(row.index('Back'), y)
            await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
            self.assertEqual(ui.state.selected_agent_id, 'ag_two')
            ui.api.action.assert_not_called()

    async def test_replacing_agent_dialog_with_quit_keeps_confirmation_focus(self):
        for target in (None, 'ag_one'):
            async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
                ui.view.composer.text = 'draft'
                menu = ui.start(ui.workflows.run_action('agents', target_id=target))
                await ui.wait_until(lambda: ui.dialogs.future is not None)
                async def quit_confirmation():
                    ui.dialogs.cancel()
                    return await ui.dialogs.confirm('Quit with draft?', default=False)
                confirmation = ui.start(quit_confirmation())
                await ui.wait_until(lambda: 'Quit with draft?' in ui.screen_text())
                await menu
                self.assertIsNot(ui.application.layout.current_control, ui.view.composer.control)
                await ui._send('n')
                await ui.wait_until(confirmation.done)
                self.assertFalse(await confirmation)
                self.assertEqual(ui.view.composer.text, 'draft')
