"""Arrow-only chooser navigation between rows and action buttons."""
import unittest

from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


def focused_text(ui):
    text = getattr(ui.application.layout.current_control, 'text', None)
    return ''.join(fragment[1] for fragment in text()) if callable(text) else ''


class ChooserArrowTests(unittest.IsolatedAsyncioTestCase):
    async def test_arrows_reach_add_and_cancel_without_tab(self):
        for agents in ([], [agent()]):
            async with workflow_harness(selected=workspace(agents=agents), size=(80, 24)) as ui:
                await ui.key('F3')
                await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
                await ui.key('Down')
                self.assertIn('Select', focused_text(ui))
                await ui.key('Right')
                self.assertIn('Add agent', focused_text(ui))
                await ui.key('Right')
                self.assertIn('Cancel', focused_text(ui))
                await ui.key('Left')
                self.assertIn('Add agent', focused_text(ui))
                await ui.key('Enter')
                await ui.wait_until(lambda: 'Provider:' in ui.screen_text())
                ui.api.action.assert_not_called()

    async def test_up_from_buttons_keeps_selected_agent_and_search_editing(self):
        async with workflow_harness(selected=workspace(agents=[agent(), agent('ag_two')])) as ui:
            await ui.key('F3')
            await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
            await ui.type_text('ag_')
            await ui.key('Left')
            await ui._send('x')
            self.assertIn('agx_', ui.screen_text())
            await ui.key('Home')
            await ui._send('\x0b')  # Ctrl+K clears search.
            await ui.key('Down')  # Highlight second agent.
            await ui.key('Down')  # Enter the button row.
            self.assertIn('Select', focused_text(ui))
            await ui.key('Right')
            await ui.key('Up')
            await ui.key('Enter')
            await ui.wait_until(lambda: 'Agent actions: ag_two' in ui.screen_text())
            self.assertEqual(ui.state.selected_agent_id, 'ag_two')
