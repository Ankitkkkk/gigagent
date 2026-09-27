"""Activity remains dismissible after unread actions and focus changes."""
import unittest

from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


class ActivityBackTests(unittest.IsolatedAsyncioTestCase):
    async def test_escape_keeps_message_focus_cursor_and_typing(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.view.focus_named('composer')
            await ui.type_text('hello world')
            await ui.key('Left')
            position = ui.view.composer.buffer.cursor_position
            await ui.key('F5')
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'composer')
            self.assertEqual(ui.state.focus_name, 'composer')
            self.assertEqual(ui.view.composer.buffer.cursor_position, position)
            await ui.key('F5')
            ui.view.focus_named('composer')
            await ui.key('Escape')
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'composer')
            self.assertEqual(ui.state.focus_name, 'composer')
            self.assertEqual(ui.view.composer.buffer.cursor_position, position)
            await ui.type_text('!')
            self.assertEqual(ui.view.composer.text, 'hello worl!d')
            self.assertFalse(ui.view.activity_visible)
            ui.api.action.assert_not_called()

    async def test_retry_unread_can_return_to_chat_from_every_pane(self):
        for focus in ('composer', 'agents', 'navigation', 'activity', 'agent_attach'):
            with self.subTest(focus=focus):
                async with workflow_harness(selected=workspace(agents=[agent(state='running')])) as ui:
                    ui.view.composer.text = 'keep draft'
                    await ui.key('F3')
                    await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
                    await ui.key('Enter')
                    await ui.wait_until(lambda: 'Agent actions:' in ui.screen_text())
                    await ui.type_text('Retry unread')
                    await ui.key('Enter')
                    await ui.wait_until(lambda: ui.view.activity_visible)
                    ui.view.focus_named(focus)
                    await ui.key('Escape')
                    self.assertFalse(ui.view.activity_visible)
                    self.assertNotIn('Activity ·', ui.rows[0])
                    self.assertEqual(ui.view.composer.text, 'keep draft')
                    ui.api.action.assert_called_once_with('ws_one', 'retry', 'ag_one')

    async def test_activity_escape_preserves_insert_newline_and_modal(self):
        async with workflow_harness(selected=workspace(agents=[agent()])) as ui:
            ui.view.show_activity()
            ui.view.focus_named('composer')
            await ui.type_text('draft')
            await ui.key('Enter')
            self.assertTrue(ui.view.activity_visible)
            self.assertEqual(ui.view.composer.text, 'draft\n')
            task = ui.start(ui.dialogs.confirm('Keep activity?'))
            await ui.wait_until(lambda: ui.dialogs.future is not None)
            await ui.key('Escape')
            self.assertFalse(await task)
            self.assertTrue(ui.view.activity_visible)
            await ui.key('Escape')
            self.assertFalse(ui.view.activity_visible)
            self.assertEqual(ui.view.composer.text, 'draft\n')
