"""Mention completions use the selected session's membership."""
import unittest

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from tests.test_cli_tui_workflows import agent, workspace, workflow_harness


def suggestions(ui, text='@'):
    return [item.text for item in ui.view.composer.buffer.completer.get_completions(
        Document(text), CompleteEvent())]


class MentionCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_members_only_including_stopped_and_orchestrator(self):
        members = [dict(agent('ag_worker'), registry_name='codex-4'),
                   dict(agent('ag_manager', state='running'), registry_name='codex-3',
                        kind='orchestrator'),
                   dict(agent('ag_stopped'), registry_name='claude-3')]
        async with workflow_harness(selected=workspace(agents=members)) as ui:
            ui.client.handle_event({'type': 'agents', 'data':
                                    ['claude-mj', 'codex-mj', 'codex-1', 'codex-3', 'codex-4']})
            self.assertEqual(suggestions(ui), ['@codex-4', '@codex-3', '@claude-3'])
            self.assertEqual(suggestions(ui, 'hello @cod'), ['@codex-4', '@codex-3'])

    async def test_empty_or_unselected_session_does_not_fall_back_to_registry(self):
        for selected in (workspace(), None):
            with self.subTest(selected=selected):
                async with workflow_harness(selected=selected) as ui:
                    ui.client.handle_event({'type': 'agents', 'data': ['outside-agent']})
                    self.assertEqual(suggestions(ui), [])

    async def test_plain_channel_retains_global_agents(self):
        async with workflow_harness(plain=True) as ui:
            ui.client.handle_event({'type': 'agents', 'data': ['global-one', 'global-two']})
            self.assertEqual(suggestions(ui), ['@global-one', '@global-two'])

    async def test_live_membership_change_dismisses_stale_menu_without_editing_draft(self):
        old = dict(agent('ag_old'), registry_name='old-local')
        async with workflow_harness(selected=workspace(agents=[old])) as ui:
            await ui._send('i@')
            await ui.wait_until(lambda: ui.view.composer.buffer.complete_state is not None)
            opened = ui.view.composer.buffer.complete_state
            # Global registry updates must not disturb a valid session menu.
            ui.client.handle_event({'type': 'agents', 'data': ['foreign-new']})
            await ui.wait_render()
            self.assertIs(ui.view.composer.buffer.complete_state, opened)
            updated = dict(old, registry_name='renamed-local')
            ui.client.handle_event({'type': 'workspace', 'data': workspace(agents=[updated])})
            await ui.wait_render()
            self.assertIsNone(ui.view.composer.buffer.complete_state)
            self.assertEqual(ui.view.composer.text, '@')
            self.assertEqual(ui.view.composer.buffer.cursor_position, 1)
            self.assertEqual(ui.view.composer_mode, 'INSERT')
            await ui._send('r')
            await ui.wait_until(lambda: ui.view.composer.buffer.complete_state is not None)
            self.assertEqual([item.text for item in ui.view.composer.buffer.complete_state.completions],
                             ['@renamed-local'])
            ui.client.handle_event({'type': 'workspace', 'data': workspace()})
            await ui.wait_render()
            self.assertIsNone(ui.view.composer.buffer.complete_state)
            self.assertEqual(suggestions(ui), [])

    async def test_switch_sessions_clears_menu_even_with_identical_draft(self):
        first = dict(agent('ag_first'), registry_name='first-local')
        second = dict(agent('ag_second'), registry_name='second-local')
        async with workflow_harness(selected=workspace(agents=[first])) as ui:
            await ui._send('i@')
            await ui.wait_until(lambda: ui.view.composer.buffer.complete_state is not None)
            ui.state.drafts.set(('session', 'ws_two'), '@', cursor=1)
            ui.controller._select(workspace('ws_two', 'Two', agents=[second]))
            ui.composer_actions.switch_draft(ui.composer_actions.destination_key(), mandatory=True)
            await ui.wait_render()
            self.assertIsNone(ui.view.composer.buffer.complete_state)
            self.assertEqual(ui.view.composer.text, '@')
            self.assertEqual(suggestions(ui), ['@second-local'])
            await ui._send('s')
            await ui.wait_until(lambda: ui.view.composer.buffer.complete_state is not None)
            self.assertEqual([item.text for item in ui.view.composer.buffer.complete_state.completions],
                             ['@second-local'])
