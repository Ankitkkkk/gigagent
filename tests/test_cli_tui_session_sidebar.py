"""Session sidebar hit targets and spacing through real terminal input."""
import unittest

from tests._tui_harness import tui_harness
from tests.test_cli_tui_workflows import workspace


class SessionSidebarTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_sidebar_clicks_never_switch_to_first_session(self):
        async with tui_harness(size=(120, 30)) as ui:
            one, two = workspace('ws_a', 'First'), workspace('ws_b', 'Second')
            ui.controller._select(two)
            ui.view.set_sessions([one, two])
            ui.state.selected_session_id = 'ws_b'
            await ui.wait_render()
            for x, y in ((2, 10), (18, 12), (6, 18)):
                await ui.click(x, y)
            self.assertEqual(ui.calls, [])
            self.assertEqual(ui.state.selected_session_id, 'ws_b')
            self.assertEqual(ui.controller.workspace['id'], 'ws_b')
            first_y, first = next((y, row) for y, row in enumerate(ui.rows) if 'First' in row[:22])
            second_y = next(y for y, row in enumerate(ui.rows) if 'Second' in row[:22])
            self.assertGreaterEqual(abs(second_y - first_y), 2)
            self.assertGreaterEqual(first.index('First'), 4)
            # Separator, top padding and right blank area must also be inert.
            for x, y in ((2, 1), (5, min(first_y, second_y) + 1)):
                await ui.click(x, y)
            self.assertEqual(ui.calls, [])
            await ui.click(first.index('First'), first_y)
            await ui.wait_until(lambda: ('run_action', 'select_session', 'ws_a') in ui.calls)
            self.assertEqual(ui.state.selected_session_id, 'ws_a')

    async def test_padded_list_scrolls_to_keyboard_selection_and_targets_visible_row(self):
        async with tui_harness(size=(120, 24)) as ui:
            rows = [workspace(f'ws_{i:02}', f'Session {i:02}') for i in range(20)]
            ui.view.set_sessions(rows)
            ui.view.focus_named('navigation')
            for _ in range(19):
                await ui.key('Down')
            self.assertEqual(ui.state.selected_session_id, 'ws_19')
            y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Session 19' in row[:22])
            await ui.click(row.index('Session 19'), y)
            await ui.wait_until(lambda: ('run_action', 'select_session', 'ws_19') in ui.calls)

    async def test_mouse_wheel_over_padded_session_row_scrolls_without_switching(self):
        async with tui_harness(size=(120, 24)) as ui:
            ui.view.set_sessions([workspace(f'ws_{i:02}', f'Session {i:02}') for i in range(20)])
            await ui.wait_render()
            y = next(y for y, row in enumerate(ui.rows) if 'Session 00' in row[:22])
            before = ui.view.navigation_window.vertical_scroll
            await ui._send(f'\x1b[<65;19;{y + 1}M')
            self.assertGreater(ui.view.navigation_window.vertical_scroll, before)
            self.assertEqual(ui.calls, [])
