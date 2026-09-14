"""Add an agent from the chooser without selecting an existing agent."""
import unittest

from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


class ChooserAddTests(unittest.IsolatedAsyncioTestCase):
    async def test_chooser_add_button_opens_form_with_empty_or_filtered_list(self):
        for rows in ([], [agent(), agent('ag_two')]):
            async with workflow_harness(selected=workspace(agents=rows), size=(80, 24)) as ui:
                ui.view.composer.text = 'keep draft'
                await ui.key('F3')
                await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
                await ui.type_text('no matching agent')
                await ui.wait_render()
                self.assertIn('No matching choices', ui.screen_text())
                # Modal button is below Search; don't click the underlying pane button.
                title_y = next(y for y, row in enumerate(ui.rows) if 'Choose agent' in row)
                y, row = next((y, row) for y, row in enumerate(ui.rows)
                              if y > title_y and '<Add agent >' in row)
                await ui.click(row.index('Add agent') + 2, y)
                await ui.wait_until(lambda: 'Provider:' in ui.screen_text())
                self.assertIsNone(ui.state.selected_agent_id)
                ui.api.action.assert_not_called()
                await ui.key('Escape')
                self.assertEqual(ui.view.composer.text, 'keep draft')

    async def test_chooser_add_button_enter_does_not_select_highlighted_agent(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            await ui.key('F3')
            await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
            # Reach the actual button via keyboard, then activate its own action.
            for _ in range(6):
                control = ui.application.layout.current_control
                text = ''.join(fragment[1] for fragment in control.text()) if callable(getattr(control, 'text', None)) else ''
                if 'Add agent' in text:
                    break
                await ui.key('Tab')
            else:
                self.fail('Add agent button is not keyboard reachable')
            await ui.key('Enter')
            await ui.wait_until(lambda: 'Provider:' in ui.screen_text())
            self.assertIsNone(ui.state.selected_agent_id)
            ui.api.action.assert_not_called()
