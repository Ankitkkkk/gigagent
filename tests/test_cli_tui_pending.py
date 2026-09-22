"""Pending-input sidebar through the real terminal renderer and key/mouse input."""
import unittest

from tests._tui_harness import tui_harness
from tests.test_cli_tui_workflows import agent, workspace


class PendingInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_list_tracks_only_active_session_and_live_clear(self):
        async with tui_harness(size=(120, 30)) as ui:
            one = workspace(agents=[dict(agent('ag_a', state='running'), waiting_for_input=True),
                                    agent('ag_ready', state='running')])
            two = workspace('ws_two', 'Two', agents=[dict(agent('ag_b', state='running'), waiting_for_input=True)])
            ui.controller._select(one)
            ui.view.set_sessions([one, two])
            ui.client.websocket = object()
            ui.client.handle_event({'type': 'status', 'data': {
                'ag_a': {'waiting_for_input': True}, 'ag_b': {'waiting_for_input': True}}})
            await ui.wait_render()
            self.assertIn('Input pending 1', ui.screen_text())
            self.assertEqual([a['agent_id'] for a in ui.view.pending_input_rows()], ['ag_a'])
            # Merely moving the session highlight must not mix its agents into the open session.
            ui.state.selected_session_id = 'ws_two'
            self.assertEqual([a['agent_id'] for a in ui.view.pending_input_rows()], ['ag_a'])
            ui.controller._select(two)
            await ui.wait_render()
            self.assertEqual([a['agent_id'] for a in ui.view.pending_input_rows()], ['ag_b'])
            ui.client.handle_event({'type': 'status', 'data': {'ag_b': {'waiting_for_input': False}}})
            await ui.wait_render()
            self.assertIn('No pending input', ui.screen_text())
            self.assertEqual(ui.view.pending_input_rows(), [])
            ui.client.websocket = None

    async def test_pending_keyboard_mouse_and_draft_preservation(self):
        async with tui_harness(size=(120, 30)) as ui:
            rows = [dict(agent(name, state='running'), waiting_for_input=True) for name in ('ag_a', 'ag_b')]
            ui.controller._select(workspace(agents=rows))
            ui.view.composer.text = 'draft stays'
            await ui.wait_render()
            ui.view.focus_named('pending_inputs')
            await ui.key('Down')
            await ui.key('Down')
            await ui.key('Enter')
            await ui.wait_until(lambda: ('run_action', 'attach', 'ag_b') in ui.calls)
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'composer')
            await ui.wait_render()
            pending_top = next(y for y, row in enumerate(ui.rows) if 'Input pending' in row[:22])
            y, row = next((y, row) for y, row in enumerate(ui.rows)
                          if y > pending_top and 'ag_a' in row[:22])
            await ui.click(row.index('ag_a'), y)
            await ui.wait_until(lambda: ('run_action', 'attach', 'ag_a') in ui.calls)
            self.assertEqual(ui.view.composer.text, 'draft stays')

    async def test_hidden_pending_panel_never_activates_after_resize(self):
        async with tui_harness(size=(120, 30)) as ui:
            ui.controller._select(workspace(agents=[dict(agent(state='running'), waiting_for_input=True)]))
            await ui.wait_render()
            ui.view.focus_named('pending_inputs')
            await ui.resize(80, 24)
            self.assertNotIn('Input pending', ui.screen_text())
            await ui.key('Down')
            await ui.key('Enter')
            ui.controller.on_workspace(dict(ui.controller.workspace, agents=[]))
            await ui.resize(120, 30)
            await ui.key('Enter')
            self.assertNotIn('attach', str(ui.calls))
            await ui.key('Escape')
            await ui.type_text('hello')
            self.assertEqual(ui.view.composer.text, 'hello')

    async def test_pending_list_scrolls_to_selection_and_handles_empty_context(self):
        async with tui_harness(size=(120, 30)) as ui:
            self.assertIn('Select a session', ui.screen_text())
            rows = [dict(agent(f'ag_{i}', state='running'), waiting_for_input=True) for i in range(12)]
            ui.controller._select(workspace(agents=rows))
            await ui.wait_render()
            self.assertIn('Input pending 12', '\n'.join(row[:22] for row in ui.rows))
            ui.view.focus_named('new_session')
            await ui.key('Tab')
            self.assertIs(ui.application.layout.current_control, ui.view.pending_inputs)
            for _ in rows:
                await ui.key('Down')
            self.assertEqual(ui.state.selected_agent_id, 'ag_11')
            self.assertIn('ag_11', '\n'.join(row[:22] for row in ui.rows))
            await ui.key('Enter')
            await ui.wait_until(lambda: ('run_action', 'attach', 'ag_11') in ui.calls)
            ui.controller._select(None)
            await ui.wait_render()
            self.assertIn('Select a session', ui.screen_text())
            self.assertEqual(ui.view.pending_input_rows(), [])
            ui.controller.plain_channel = True
            ui.view.focus_named('composer')
            await ui.wait_render()
            self.assertNotIn('Input pending', ui.screen_text())
