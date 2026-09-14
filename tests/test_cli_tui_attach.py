"""Direct terminal entry points preserve agent selection and composer state."""
import unittest

from tests._tui_harness import tui_harness
from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


class DirectAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_f6_always_chooses_even_after_selecting_or_attaching_agent(self):
        for count in (1, 2):
            rows = [agent('ag_a', state='running'), agent('ag_b', state='running')][:count]
            async with workflow_harness(selected=workspace(agents=rows), size=(80, 24)) as ui:
                ui.state.selected_agent_id = rows[-1]['agent_id']
                ui.view.composer.text = 'keep draft'
                for attempt in range(2):
                    ui.pipe.send_text('\x1b[17~')
                    await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
                    for row in rows:
                        self.assertIn(row['registry_name'], ui.screen_text())
                    self.assertEqual(len([call for call in ui.calls if call[0] == 'attach']), attempt)
                    await ui.key('Enter')
                    await ui.wait_until(lambda: len([call for call in ui.calls if call[0] == 'attach']) == attempt + 1)
                    self.assertEqual(ui.calls[-1], ('attach', 'ag_a'))
                ui.pipe.send_text('\x1b[17~')
                await ui.wait_until(lambda: 'Choose agent' in ui.screen_text())
                await ui.key('Escape')
                await ui.wait_until(lambda: ui.dialogs.future is None)
                self.assertEqual(len([call for call in ui.calls if call[0] == 'attach']), 2)
                self.assertEqual(ui.view.composer.text, 'keep draft')

    async def test_button_attaches_selected_agent_and_shortcut_requests_choice(self):
        for size in ((120, 30), (80, 24)):
            async with tui_harness(size=size) as ui:
                ui.controller._select(workspace(agents=[agent('ag_a', state='running'),
                                                       agent('ag_b', state='running')]))
                ui.state.selected_agent_id = 'ag_b'
                ui.view.composer.text = 'unsent draft'
                await ui.wait_render()
                self.assertIn('Attach', ui.screen_text())
                y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Attach' in row)
                await ui.click(row.index('Attach') + 2, y)
                await ui.wait_until(lambda: ('run_action', 'attach', 'ag_b') in ui.calls)
                ui.view.focus_named('agents')
                await ui.key('Tab')
                await ui.key('Enter')
                await ui.wait_until(lambda: ui.calls.count(('run_action', 'attach', 'ag_b')) == 2)
                await ui.key('Escape')
                self.assertEqual(ui.focused_control, 'composer')
                ui.pipe.send_text('\x1b[17~')  # F6
                await ui.wait_until(lambda: ('run_action', 'choose_attach', None) in ui.calls)
                self.assertEqual(ui.calls.count(('run_action', 'attach', 'ag_b')), 2)
                self.assertEqual(ui.view.composer.text, 'unsent draft')
                self.assertNotIn(('run_action', 'agents', 'ag_b'), ui.calls)

    async def test_shortcut_delegates_choice_and_ignores_hidden_contexts(self):
        async with tui_harness(size=(120, 30)) as ui:
            ui.controller._select(workspace(agents=[agent('ag_a'), agent('ag_b')]))
            await ui.wait_render()
            ui.pipe.send_text('\x1b[17~')
            await ui.wait_until(lambda: ('run_action', 'choose_attach', None) in ui.calls)
            ui.calls.clear()
            ui.view.show_help('Help')
            ui.pipe.send_text('\x1b[17~')
            await ui.wait_render()
            await ui.key('Escape')
            await ui.resize(70, 15)
            ui.pipe.send_text('\x1b[17~')
            await ui.wait_render()
            await ui.resize(120, 30)
            ui.controller._select(None)
            ui.pipe.send_text('\x1b[17~')
            await ui.wait_render()
            self.assertFalse(ui.view._show_attach())
            ui.controller._select(workspace(agents=[agent()]))
            ui.controller.plain_channel = True
            ui.pipe.send_text('\x1b[17~')
            await ui.wait_render()
            self.assertEqual(ui.calls, [])

    async def test_disappearing_attach_button_returns_focus_to_message(self):
        async with tui_harness(size=(120, 30)) as ui:
            ui.controller._select(workspace(agents=[agent(state='running')]))
            await ui.wait_render()
            ui.view.focus_named('agents')
            await ui.key('Tab')
            ui.controller.on_workspace(dict(ui.controller.workspace, agents=[]))
            await ui.wait_render()
            self.assertEqual(ui.focused_control, 'composer')
            await ui.type_text('hello')
            self.assertEqual(ui.view.composer.text, 'hello')
