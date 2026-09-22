"""Pane placement and interaction across terminal sizes."""

import unittest

from tests._tui_harness import tui_harness
from tests.test_cli_tui_editing import bind


class PaneLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_minimum_wide_rail_keeps_selected_agent_and_actions_visible(self):
        async with tui_harness(size=(110, 24)) as ui:
            bind(ui)
            ui.controller.on_workspace(dict(ui.controller.workspace, agents=[{
                'agent_id': f'ag_{index}', 'registry_name': f'worker-{index}',
                'last_state': 'running', 'tmux_session': f'inert-{index}'}
                for index in range(12)]))
            ui.view.focus_named('agents')
            for _ in range(12):
                await ui.key('Down')
            self.assertEqual(ui.state.selected_agent_id, 'ag_11')
            self.assertIn('worker-11', ui.screen_text())
            selected_row = next(y for y, row in enumerate(ui.rows) if 'worker-11' in row)
            self.assertIn('● Ready', ui.rows[selected_row + 1][:22])
            self.assertNotIn('Window too small', ui.screen_text())
            for caption in ('Attach', 'Actions', 'Add agent'):
                self.assertIn(caption, ui.screen_text())
            await ui.key('Tab')
            await ui.key('Enter')
            await ui.wait_until(lambda: ('run_action', 'attach', 'ag_11') in ui.calls)

    async def test_blank_agent_rail_click_does_not_change_selected_agent(self):
        async with tui_harness(size=(120, 40)) as ui:
            bind(ui)
            ui.controller.on_workspace(dict(ui.controller.workspace, agents=[
                {'agent_id': name, 'registry_name': name, 'last_state': 'running'}
                for name in ('first-worker', 'second-worker')]))
            ui.state.selected_agent_id = 'first-worker'
            await ui.wait_render()
            info = ui.view.agents_window.render_info
            y = info._y_offset + info.window_height - 1
            self.assertFalse(ui.rows[y][1:21].strip())
            await ui.click(10, y)
            self.assertEqual(ui.state.selected_agent_id, 'first-worker')
            self.assertEqual(ui.calls, [])

    async def test_wide_agent_rail_leaves_chat_and_editor_in_one_column(self):
        async with tui_harness(size=(120, 35)) as ui:
            bind(ui)
            ui.controller.on_workspace(dict(ui.controller.workspace, agents=[{
                'agent_id': 'ag_worker', 'registry_name': 'worker-one',
                'last_state': 'running'}]))
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 1, 'sender': 'user', 'text': 'conversation marker'}})
            await ui.paste('draft marker')
            y, row = next((y, row) for y, row in enumerate(ui.rows) if 'worker-one' in row)
            chat = next(row for row in ui.rows if 'conversation marker' in row)
            draft = next(row for row in ui.rows if 'draft marker' in row)
            self.assertLess(row.index('worker-one'), chat.index('conversation marker'))
            self.assertLess(row.index('worker-one'), draft.index('draft marker'))
            self.assertEqual(ui.view.agents_window.render_info.window_width,
                             ui.view.navigation_window.render_info.window_width)
            await ui.click(row.index('worker-one'), y)
            self.assertEqual(ui.focused_control, 'agents')
            await ui.key('Enter')
            await ui.wait_until(lambda: ('run_action', 'agents', 'ag_worker') in ui.calls)
            await ui.key('Escape')
            await ui._send('Z')
            self.assertEqual(ui.view.composer.text, 'draft markerZ')

    async def test_agent_controls_survive_compact_resize_and_copy_return(self):
        async with tui_harness(size=(120, 35)) as ui:
            bind(ui)
            ui.controller.on_workspace(dict(ui.controller.workspace, agents=[{
                'agent_id': 'ag_worker', 'registry_name': 'worker-one',
                'last_state': 'running'}]))
            await ui.paste('kept draft')
            await ui.key('Left')
            for width, height in ((80, 24), (160, 40)):
                await ui.resize(width, height)
                self.assertIn('worker-one', ui.screen_text())
                await ui.activate_named('agent_actions')
                self.assertIn(('run_action', 'agents', None), ui.calls)
                await ui.key('Escape')
                await ui._send('\x1b[18~')
                await ui._send('\x1b[18~')
                self.assertEqual(ui.focused_control, 'composer')
                self.assertEqual(ui.view.composer.buffer.cursor_position, 9)
            await ui._send('Z')
            self.assertEqual(ui.view.composer.text, 'kept drafZt')
