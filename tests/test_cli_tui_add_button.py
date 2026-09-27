"""Visible session-level Add agent entry, independent of agent selection."""
import unittest

from tests._tui_harness import tui_harness
from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


class AddAgentButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_button_opens_form_without_selecting_an_agent(self):
        for size in ((120, 30), (80, 24)):
            for rows in ([], [agent('ag_a'), agent('ag_b')]):
                one = workspace(agents=rows)
                async with workflow_harness(rows=[one], selected=one, size=size) as ui:
                    ui.view.composer.text = 'draft survives'
                    await ui.wait_render()
                    info = ui.view.agent_add.window.render_info
                    self.assertIn('+ Add agent', ui.rows[info._y_offset])
                    await ui.click(info._x_offset + 5, info._y_offset)
                    await ui.wait_until(lambda: 'Provider' in ui.screen_text())
                    self.assertNotIn('Choose agent', ui.screen_text())
                    await ui.key('Escape')
                    self.assertEqual(ui.view.composer.text, 'draft survives')
                    self.assertIsNone(ui.state.selected_agent_id)

    async def test_add_button_keyboard_and_session_removal_restore_message(self):
        async with tui_harness(size=(120, 30)) as ui:
            ui.controller._select(workspace())
            await ui.wait_render()
            ui.view.focus_named('agent_actions')
            await ui.key('Tab')
            await ui.key('Enter')
            await ui.wait_until(lambda: ('run_action', 'new_agent', None) in ui.calls)
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'composer')
            ui.view.focus_named('agent_add')
            await ui.resize(70, 15)
            ui.controller._select(None)
            await ui.resize(120, 30)
            await ui.type_text('hello')
            self.assertEqual(ui.view.composer.text, 'hello')
            self.assertNotIn('+ Add agent', ui.screen_text())
