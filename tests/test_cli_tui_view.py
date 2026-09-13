"""Real renderer checks for the persistent terminal view."""

import unittest

from cli import ChatClient
from cli_workspace_chat import WorkspaceChatController
from tests._tui_harness import tui_harness


class ViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_agent_records_are_skipped_by_real_renderer(self):
        async with tui_harness() as ui:
            ui.controller._select({'id': 'ws_one', 'name': 'One', 'channel': 'general', 'agents': []})
            ui.controller.workspace['agents'] = [None, {}, {'agent_id': ''},
                {'agent_id': 'ag_good', 'registry_name': 'valid-agent', 'last_state': 'exited'}]
            await ui.wait_render()
            self.assertIn('valid-agent', ui.screen_text())
            ui.view.focus_named('agents')
            await ui.key('Down')
            await ui.key('Enter')
            self.assertIn('ag_good', ui.screen_text())

    async def test_compact_resize_keeps_draft_and_focus(self):
        async with tui_harness(size=(120, 30)) as ui:
            composer = ui.view.composer
            controls = (ui.view.conversation, ui.view.navigation, ui.view.agents)
            composer.text = 'draft survives'
            composer.buffer.cursor_position = 5
            ui.view.focus_named('composer')
            await ui.resize(80, 24)
            self.assertEqual(ui.view.screen_mode, 'compact')
            self.assertEqual(composer.text, 'draft survives')
            self.assertIn('F2 Sessions', ui.screen_text())
            await ui.resize(70, 16)
            self.assertIn('Resize', ui.screen_text())
            await ui.resize(120, 30)
            self.assertEqual(ui.focused_control, 'composer')
            self.assertIs(ui.view.composer, composer)
            self.assertEqual(composer.buffer.cursor_position, 5)
            self.assertEqual(controls, (ui.view.conversation, ui.view.navigation, ui.view.agents))

    async def test_exact_width_and_height_breakpoints(self):
        async with tui_harness() as ui:
            for size, mode in [((110, 24), 'wide'), ((109, 24), 'compact'),
                               ((110, 23), 'compact'), ((80, 18), 'compact'),
                               ((79, 18), 'small'), ((80, 17), 'small')]:
                await ui.resize(*size)
                self.assertEqual(ui.view.screen_mode, mode)
                self.assertIn('Resize' if mode == 'small' else 'Message', ui.screen_text())
            await ui.resize(120, 30)
            self.assertEqual(ui.cell(21, 2), '│')
            self.assertIn('New session', ui.screen_text())

    async def test_real_events_replace_delete_and_order_messages_once(self):
        async with tui_harness() as ui:
            for ident, timestamp, text in [(8, 3, 'third'), (9, 1, 'first'), (2, 2, 'second')]:
                ui.client.handle_event({'type': 'message', 'data': {
                    'id': ident, 'timestamp': timestamp, 'time': '12:00',
                    'sender': 'human', 'text': text}})
            await ui.wait_render()
            screen = ui.screen_text()
            self.assertIn('first', screen)
            self.assertLess(screen.index('first'), screen.index('second'))
            self.assertLess(screen.index('second'), screen.index('third'))
            self.assertEqual(screen.count('third'), 1)
            self.assertEqual(ui.view.state.viewport.anchor_id, 8)
            ui.client.handle_event({'type': 'message_update', 'message': {
                'id': 2, 'timestamp': 2, 'sender': 'human', 'text': 'replacement'}})
            await ui.wait_render()
            self.assertNotIn('second', ui.screen_text())
            self.assertEqual(ui.screen_text().count('replacement'), 1)
            ui.client.handle_event({'type': 'delete', 'ids': [2]})
            await ui.wait_render()
            self.assertNotIn('replacement', ui.screen_text())
            self.assertEqual(len(ui.view.state.notices), 0)

    async def test_unicode_long_lines_and_untrusted_fragments_are_safe(self):
        async with tui_harness(size=(80, 24)) as ui:
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 1, 'sender': '\x1b[31m坏\u202e', 'time': '\x07now',
                'text': '界' * 85 + '\nline\tend\x00\x1b[2J\u200b',
                'attachments': [{'name': 'file\x1b\u202e.txt', 'url': 'https://local/\x07file'}],
                'metadata': {'choices': ['yes\x1b', 'no\u202e']}}})
            await ui.wait_render()
            screen = ui.screen_text()
            source = ''.join(fragment[1] for fragment in ui.view.conversation.text())
            for forbidden in ('\x1b', '\x00', '\x07', '\u202e', '\u200b'):
                self.assertNotIn(forbidden, source)
            for rendered_control in ('^[[', '^@', '^G'):
                self.assertNotIn(rendered_control, screen)
            self.assertIn('界', screen)
            self.assertIn('line    end[2J', screen)
            self.assertIn('file.txt', screen)
            self.assertIn('https://local/file', screen)
            self.assertIn('Choices: yes | no', screen)

    async def test_plain_channel_has_channels_without_lifecycle(self):
        client = ChatClient('http://127.0.0.1:18300')
        controller = WorkspaceChatController(client, None, plain_channel=True)
        async with tui_harness(client=client, controller=controller) as ui:
            client.handle_event({'type': 'settings', 'data': {'channels': ['general', '开发']}})
            await ui.wait_render()
            self.assertIn('Channels', ui.screen_text())
            self.assertIn('开发', ui.screen_text())
            for label in ('Add agent', 'Stop agent', 'Resume agent'):
                self.assertNotIn(label, ui.screen_text())

    async def test_pipe_keys_delegate_callbacks_and_preserve_text(self):
        async with tui_harness() as ui:
            await ui.type_text('raw draft')
            self.assertEqual(ui.view.composer.text, 'raw draft')
            await ui.key('F2')
            await ui.wait_until(lambda: ('navigate', False) in ui.calls)
            await ui.key('F3')
            await ui.wait_until(lambda: ('run_action', 'agents', None) in ui.calls)
            await ui.key('F4')
            await ui.wait_until(lambda: ('run_action', 'commands', None) in ui.calls)
            await ui.key('F1')
            await ui.wait_until(lambda: ('run_action', 'help', None) in ui.calls)
            await ui.key('CtrlC')
            self.assertEqual(ui.view.composer.text, 'raw draft')
            await ui.key('CtrlQ')
            await ui.wait_until(lambda: ('quit',) in ui.calls)

    async def test_navigation_metadata_sort_suffixes_and_full_id_activation(self):
        async with tui_harness() as ui:
            rows = [{'id': 'ws_111111', 'name': '重复', 'updated_at': '2026-09-12',
                     'archived': True, 'agents': [{'sensitive': 'not navigation'}]},
                    {'id': 'ws_222222', 'name': '重复', 'updated_at': '2026-09-13', 'archived': False}]
            ui.view.set_sessions(rows)
            ui.view.focus_named('navigation')
            await ui.wait_render()
            screen = ui.screen_text()
            self.assertLess(screen.index('222222'), screen.index('111111'))
            self.assertIn('archived', screen)
            self.assertEqual(ui.cell(21, 2), '│')
            await ui.key('Down')
            self.assertEqual(ui.state.selected_session_id, 'ws_111111')
            await ui.key('Enter')
            await ui.wait_until(lambda: ('run_action', 'select_session', 'ws_111111') in ui.calls)
            self.assertIsNone(ui.controller.workspace)
            ui.view.set_sessions(list(reversed(rows)))
            self.assertEqual(ui.state.selected_session_id, 'ws_111111')
            self.assertNotIn('agents', ui.view._sessions[0])

    async def test_agent_events_and_inspector_keep_recovery_text_without_chat_noise(self):
        async with tui_harness() as ui:
            agent = {'agent_id': 'ag_full', 'registry_name': 'worker\x1b\u202e',
                     'provider': 'codex', 'last_state': 'starting', 'last_launch': {'kind': 'fresh'},
                     'history_mode': 'literal', 'history_state': 'pending', 'cwd': None,
                     'last_error': 'failure\x00' + ' details' * 25}
            ui.controller._select({'id': 'ws_one', 'name': 'Session', 'channel': 'general',
                                   'agents': [agent]})
            ui.state.selected_agent_id = 'ag_full'
            await ui.wait_render()
            self.assertIn('fresh', ui.screen_text())
            self.assertIn('id unknown', ui.screen_text())
            self.assertIn('catching up…', ui.view.inspector_text())
            self.assertIn("⚠ cwd missing — /resume 'worker' --cwd PATH", ui.view.inspector_text())
            ui.view.show_inspector()
            await ui.wait_render()
            self.assertIn('Agent details', ui.screen_text())
            self.assertIn('ag_full', ui.screen_text())
            ui.client.handle_event({'type': 'workspace', 'data': dict(ui.controller.workspace,
                agents=[dict(agent, last_state='running')])})
            await ui.wait_render()
            self.assertIn('running', ui.screen_text())
            self.assertEqual(len(ui.client.messages), 0)
            self.assertEqual(len(ui.state.notices), 0)
            self.assertNotIn('\x00', ui.view.inspector_text())

    async def test_disabled_reasons_derive_from_current_models(self):
        from unittest.mock import patch
        from cli_workspaces import WINDOWS_TMUX_ERROR
        async with tui_harness() as ui:
            choices = {row['id']: row for row in ui.view.action_choices()}
            self.assertEqual(choices['new_session']['disabled_reason'], '')
            self.assertEqual(choices['new_agent']['disabled_reason'], 'Select a session first')
            ui.controller._select({'id': 'ws_one', 'name': 'Session', 'channel': 'general',
                                   'agents': []})
            choices = {row['id']: row for row in ui.view.action_choices()}
            self.assertEqual(choices['stop']['disabled_reason'], 'Select an agent first')
            with patch('cli_tui_view.sys.platform', 'win32'):
                choices = {row['id']: row for row in ui.view.action_choices()}
                self.assertEqual(choices['new_agent']['disabled_reason'], WINDOWS_TMUX_ERROR)
            ui.controller.plain_channel = True
            ids = {row['id'] for row in ui.view.action_choices()}
            self.assertIn('create_channel', ids)
            self.assertNotIn('new_agent', ids)

    async def test_anchored_viewport_new_counter_updates_and_deletion(self):
        async with tui_harness() as ui:
            for ident in range(20):
                ui.client.handle_event({'type': 'message', 'data': {
                    'id': ident, 'timestamp': ident, 'text': f'body-{ident}'}})
            ui.state.viewport.anchor(3, ui.client.messages)
            ui.view.refresh()
            await ui.wait_render()
            self.assertIn('body-3', ui.screen_text())
            ui.client.handle_event({'type': 'message', 'data': {'id': 20, 'timestamp': 20, 'text': 'new-body'}})
            await ui.wait_render()
            self.assertIn('1 new', ui.screen_text())
            self.assertNotIn('new-body', ui.screen_text())
            ui.client.handle_event({'type': 'message_update', 'message': {
                'id': 20, 'timestamp': 20, 'text': 'edited-body'}})
            await ui.wait_render()
            self.assertIn('1 new', ui.screen_text())
            ui.client.handle_event({'type': 'delete', 'ids': [3]})
            await ui.wait_render()
            self.assertEqual(ui.state.viewport.anchor_id, 4)
            self.assertIn('body-4', ui.screen_text())

    async def test_small_surface_blocks_hidden_editing_and_preserves_navigation_focus(self):
        async with tui_harness() as ui:
            ui.view.focus_named('navigation')
            await ui.resize(70, 16)
            await ui.type_text('hidden input')
            await ui.key('Tab')
            await ui.resize(120, 30)
            self.assertEqual(ui.focused_control, 'navigation')
            self.assertEqual(ui.view.composer.text, '')
            ui.view.focus_named('composer')
            await ui.resize(70, 16)
            await ui.type_text('hidden input')
            await ui.resize(120, 30)
            self.assertEqual(ui.view.composer.text, '')

    async def test_loading_preserves_rows_and_current_title_uses_controller(self):
        async with tui_harness() as ui:
            ui.view.set_sessions([{'id': 'ws_one', 'name': 'Stale title', 'updated_at': '2026-09-13'}])
            ui.controller._select({'id': 'ws_one', 'name': 'Current title', 'channel': 'general', 'agents': []})
            ui.view.set_sessions_loading(True)
            await ui.wait_render()
            self.assertIn('Loading sessions…', ui.screen_text())
            self.assertIn('Current title', ui.screen_text())
            self.assertNotIn('Stale title', ui.screen_text())
            ui.view.set_sessions_loading(False)
            await ui.wait_render()
            self.assertNotIn('Loading sessions', ui.screen_text())

    async def test_row_target_is_captured_before_async_callback_runs(self):
        import asyncio
        async with tui_harness() as ui:
            ui.view.set_sessions([{'id': 'ws_one', 'name': 'One'}, {'id': 'ws_two', 'name': 'Two'}])
            ui.view.focus_named('navigation')
            original = ui.application.create_background_task
            scheduled = []

            def defer(coroutine):
                if coroutine.cr_code.co_name == 'run_action':
                    scheduled.append(coroutine)
                    return None
                return original(coroutine)

            ui.application.create_background_task = defer
            try:
                await ui.key('Enter')
                ui.state.selected_session_id = 'ws_two'
            finally:
                ui.application.create_background_task = original
            self.assertEqual(len(scheduled), 1)
            await asyncio.gather(*scheduled)
            self.assertIn(('run_action', 'select_session', 'ws_one'), ui.calls)

    async def test_safe_composer_display_retains_raw_value_and_cursor(self):
        async with tui_harness() as ui:
            raw = 'draft\x1b[31m\u202e\x00界'
            ui.view.composer.text = raw
            ui.view.composer.buffer.cursor_position = len(raw)
            await ui.wait_render()
            self.assertIn('draft[31m界', ui.screen_text())
            self.assertEqual(ui.view.composer.text, raw)
            self.assertEqual(ui.view.composer.buffer.cursor_position, len(raw))
            for char in ('\x1b', '\u202e', '\x00'):
                self.assertNotIn(char, ui.screen_text())

    async def test_real_dialog_disabled_reason_and_escape_timeout(self):
        import asyncio
        async with tui_harness() as ui:
            task = asyncio.create_task(ui.dialogs.choose('Commands', ui.view.action_choices()))
            try:
                await ui.wait_until(lambda: ui.dialogs.future is not None)
                await ui.type_text('Add agent')
                await ui.wait_until(lambda: 'Select a session first' in ui.screen_text())
                await ui.key('Enter')
                self.assertFalse(task.done())
                # Escape's 50ms VT decoder timeout is below composer timeoutlen=1s.
                await asyncio.wait_for(ui.key('Escape'), 0.5)
                result = await asyncio.wait_for(task, 0.5)
                self.assertTrue(result.cancelled)
                self.assertEqual(ui.focused_control, 'composer')
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_history_bound_and_renderer_show_latest_without_duplicate_records(self):
        async with tui_harness() as ui:
            ui.client.handle_event({'type': 'history', 'messages': [
                {'id': ident, 'timestamp': ident, 'sender': 'human', 'text': f'record-{ident}'}
                for ident in range(10005)]})
            ui.client.handle_event({'type': 'history_complete'})
            await ui.wait_render()
            self.assertEqual(len(ui.client.messages), 10000)
            self.assertIn('record-10004', ui.screen_text())
            self.assertEqual(ui.screen_text().count('record-10004'), 1)
            self.assertEqual(ui.state.viewport.anchor_id, 10004)
            self.assertEqual(len(ui.state.notices), 0)

    async def test_harness_initial_session_fetch_notice_and_hook_cleanup(self):
        from unittest.mock import Mock
        client = ChatClient('http://127.0.0.1:18300')
        api = Mock()
        api.list.return_value = {'workspaces': [{'id': 'ws_initial', 'name': 'Initial session'}],
                                 'warning': 'fetch warning\x00'}
        controller = WorkspaceChatController(client, api)
        old_hook, old_output = client.on_view_change, client.output
        async with tui_harness(client=client, controller=controller) as ui:
            self.assertIn('Initial session', ui.screen_text())
            api.list.assert_called_once_with(include_archived=True)
            self.assertEqual(ui.state.notices.lines, ('fetch warning',))
            client.show('incoming notice')
            await ui.wait_render()
            self.assertEqual(ui.state.notices.lines[-1], 'incoming notice')
        self.assertIs(client.on_view_change, old_hook)
        self.assertIs(client.output, old_output)
        self.assertIsNone(controller.presentation)
        self.assertTrue(ui.task.done())

    async def test_focus_border_uses_cyan_and_default_background(self):
        async with tui_harness() as ui:
            screen = ui.application.renderer.last_rendered_screen
            composer = ui.view.composer.window.render_info
            row = composer._y_offset - 1
            border = screen.data_buffer[row][22]
            attrs = ui.view.style.get_attrs_for_style_str(border.style)
            self.assertEqual(attrs.color, 'ansicyan')
            self.assertEqual(attrs.bgcolor, '')

    async def test_inspector_scrolling_reaches_full_recovery_log(self):
        async with tui_harness(size=(80, 24)) as ui:
            agent = {'agent_id': 'ag_log', 'registry_name': 'worker', 'provider': 'codex',
                     'last_state': 'exited', 'last_error': 'Beginning ' + 'details ' * 220 + 'LOG-TAIL',
                     'cwd': None}
            ui.controller._select({'id': 'ws_one', 'name': 'Session', 'channel': 'general', 'agents': [agent]})
            ui.state.selected_agent_id = 'ag_log'
            ui.view.focus_named('agents')
            await ui.key('Enter')
            self.assertIn('Agent details', ui.screen_text())
            for _ in range(70):
                await ui.key('Down')
                if 'LOG-TAIL' in ui.screen_text():
                    break
            self.assertIn('LOG-TAIL', ui.screen_text())
            await ui.key('Escape')
            self.assertFalse(ui.view.inspecting)
            self.assertIn('Agents', ui.screen_text())

    async def test_tiny_surface_shows_only_available_shortcuts(self):
        async with tui_harness(size=(70, 16)) as ui:
            self.assertIn('F1 Help', ui.screen_text())
            self.assertIn('Ctrl+Q Quit', ui.screen_text())
            self.assertNotIn('F2 Sessions', ui.screen_text())
            self.assertNotIn('F4 Commands', ui.screen_text())

    async def test_real_controller_presenter_uses_controlled_attach_and_dialog(self):
        import asyncio
        async with tui_harness() as ui:
            agent = {'agent_id': 'ag_stable', 'last_state': 'running', 'provider': 'codex'}
            ui.controller._select({'id': 'ws_one', 'name': 'Session', 'channel': 'general', 'agents': [agent]})
            result = await ui.controller.execute_action('attach', {'agent_id': 'ag_stable'})
            self.assertEqual(result.status, 'cancelled')
            self.assertIn(('attach', 'ag_stable'), ui.calls)
            task = asyncio.create_task(ui.controller.presentation.confirm('Continue?', default=False))
            try:
                await ui.wait_until(lambda: ui.dialogs.future is not None)
                await ui.key('Enter')
                self.assertFalse(await asyncio.wait_for(task, 2))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_new_session_control_works_empty_and_with_rows(self):
        async with tui_harness() as ui:
            for rows in ([], [{'id': 'ws_one', 'name': 'Existing'}]):
                ui.view.set_sessions(rows)
                ui.view.focus_named('navigation')
                await ui.key('Tab')
                self.assertEqual(ui.focused_control, 'new_session')
                await ui.key('Enter')
                await ui.wait_until(lambda: ('run_action', 'new_session', None) in ui.calls)
                self.assertIn('New session', ui.screen_text())
                ui.calls.clear()

    async def test_new_session_button_accepts_real_mouse_input(self):
        async with tui_harness() as ui:
            ui.view.set_sessions([{'id': 'ws_one', 'name': 'Existing'}])
            await ui.wait_render()
            position = next((x, y) for y, line in enumerate(ui.rows)
                            if (x := line.find('New session')) >= 0)
            await ui.click(*position)
            await ui.wait_until(lambda: ('run_action', 'new_session', None) in ui.calls)
            self.assertIsNone(ui.controller.workspace)

    async def test_connection_caption_and_small_shortcuts_are_clear(self):
        async with tui_harness() as ui:
            ui.client._set_connection_state('connected')
            await ui.wait_render()
            self.assertIn('Connected', ui.screen_text())
            await ui.resize(70, 16)
            self.assertEqual(ui.screen_text().count('F1 Help'), 1)
            self.assertEqual(ui.screen_text().count('Ctrl+Q Quit'), 1)

    async def test_hidden_sidebar_controls_do_not_activate_after_resize(self):
        async with tui_harness() as ui:
            ui.view.set_sessions([{'id': 'ws_one', 'name': 'One'}, {'id': 'ws_two', 'name': 'Two'}])
            ui.view.focus_named('navigation')
            await ui.resize(80, 24)
            await ui.key('Down')
            await ui.key('Enter')
            self.assertEqual(ui.state.selected_session_id, 'ws_one')
            self.assertEqual(ui.calls, [])
            await ui.resize(120, 30)
            ui.view.focus_named('new_session')
            for size in ((80, 24), (70, 16)):
                await ui.resize(*size)
                await ui.key('Enter')
                self.assertEqual(ui.calls, [])
            await ui.resize(120, 30)
            self.assertEqual(ui.focused_control, 'new_session')

    async def test_shutdown_after_exit_cancels_dialog_and_restores_hooks(self):
        import asyncio
        async with tui_harness() as ui:
            waiter = asyncio.create_task(ui.dialogs.confirm('Pending shutdown?', escape=False))
            await ui.wait_until(lambda: ui.dialogs.future is not None)
            ui.application.exit()
        self.assertFalse(await asyncio.wait_for(waiter, 0.5))
        self.assertIsNone(ui.dialogs.future)
        self.assertTrue(ui.task.done())
        self.assertIsNone(ui.client.on_view_change)
        self.assertIsNone(ui.controller.presentation)

    async def test_small_global_help_quit_do_not_quote_control_into_draft(self):
        import asyncio
        async with tui_harness(size=(70, 16)) as ui:
            ui.view.composer.text = 'safe draft'
            await ui.key('CtrlQ')
            self.assertIn(('quit',), ui.calls)
            original = ui.callbacks['run_action']
            async def help_action(action_id, *, target_id=None):
                if action_id == 'help':
                    await ui.dialogs.confirm('Keyboard help', escape=False)
                else:
                    return await original(action_id, target_id=target_id)
            ui.callbacks['run_action'] = help_action
            await ui.key('F1')
            await ui.wait_until(lambda: 'Keyboard help' in ui.screen_text())
            await asyncio.wait_for(ui.key('Escape'), 0.5)
            await ui.resize(120, 30)
            await ui.key('CtrlQ')
            self.assertEqual(ui.view.composer.text, 'safe draft')
            self.assertNotIn('\x11', ui.view.composer.text)

    async def test_single_resize_render_keeps_latest_and_wraps_new_width(self):
        from prompt_toolkit.data_structures import Size
        async with tui_harness(size=(120, 40)) as ui:
            ui.client.handle_event({'type': 'history', 'messages': [
                {'id': ident, 'timestamp': ident, 'text': f'message-{ident}'} for ident in range(30)]})
            ui.client.handle_event({'type': 'history_complete'})
            await ui.wait_render()
            before = ui.render_count
            ui.size = Size(rows=24, columns=120)
            ui.application._on_resize()
            self.assertEqual(ui.render_count, before + 1)
            self.assertIn('message-29', ui.screen_text())
            ui.client.handle_event({'type': 'clear'})
            ui.client.handle_event({'type': 'message', 'data': {'id': 40, 'text': 'x' * 85 + 'TAIL'}})
            await ui.wait_render()
            before = ui.render_count
            ui.size = Size(rows=24, columns=80)
            ui.application._on_resize()
            self.assertEqual(ui.render_count, before + 1)
            self.assertIn('TAIL', ui.screen_text())
            self.assertIn('x' * 7 + 'TAIL', ui.screen_text())

    async def test_follow_render_has_bounded_work_and_coarse_keystroke_latency(self):
        from time import perf_counter
        from unittest.mock import patch
        import cli_tui_view
        async with tui_harness() as ui:
            ui.client.handle_event({'type': 'history', 'messages': [
                {'id': ident, 'timestamp': ident, 'text': 'x' * 200} for ident in range(10000)]})
            ui.client.handle_event({'type': 'history_complete'})
            await ui.wait_render()
            with patch('cli_tui_view._wrap', wraps=cli_tui_view._wrap) as wrapping, \
                    patch('cli_tui_view.channel_transcript', wraps=cli_tui_view.channel_transcript) as ordering:
                started = perf_counter()
                await ui.type_text('x')
                elapsed = perf_counter() - started
                self.assertLess(wrapping.call_count, 30, 'one keypress must wrap only visible-tail records')
                self.assertEqual(ordering.call_count, 0, 'a keypress must reuse ordered ID metadata')
                # Coarse environment-sensitive guard, paired with deterministic bounded work.
                self.assertLess(elapsed, 0.1, f'coarse keypress guard: {elapsed:.3f}s')

    async def test_activity_retains_all_notices_and_preserves_chat_context(self):
        async with tui_harness() as ui:
            for ident in range(1002):
                ui.client.show(f'notice-{ident}\x1b\x00')
            await ui.wait_render()
            self.assertIn('notice-1001', ui.screen_text())
            self.assertIn('F5 Activity', ui.screen_text())
            ui.view.composer.text = 'unsent'
            ui.view.composer.buffer.cursor_position = 2
            ui.state.viewport.follow = False
            ui.state.viewport.anchor_id = 7
            before = (ui.state.viewport.follow, ui.state.viewport.anchor_id,
                      set(ui.state.viewport.new_ids), ui.view.composer.buffer.cursor_position)
            await ui.key('F5')
            self.assertIn('Activity', ui.screen_text())
            self.assertIn('2 omitted', ui.screen_text())
            self.assertIn('notice-2', ui.screen_text())
            self.assertNotIn('^[', ui.screen_text())
            await ui.key('End')
            self.assertEqual(ui.screen_text().count('notice-1001'), 2)
            await ui.key('Home')
            self.assertIn('notice-2', ui.screen_text())
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'composer')
            self.assertEqual(ui.view.composer.text, 'unsent')
            self.assertEqual(before, (ui.state.viewport.follow, ui.state.viewport.anchor_id,
                                     ui.state.viewport.new_ids, ui.view.composer.buffer.cursor_position))

    async def test_session_error_keeps_rows_and_exposes_stale_marker_notice_once(self):
        async with tui_harness() as ui:
            ui.view.set_sessions([{'id': 'ws_one', 'name': 'Existing'}])
            ui.view.set_sessions_loading(True)
            ui.view.set_sessions_error('Refresh failed\x1b')
            await ui.wait_render()
            self.assertIn('Stale', ui.screen_text())
            self.assertIn('Existing', ui.screen_text())
            self.assertIn('Refresh failed', ui.screen_text())
            self.assertEqual(ui.state.notices.lines, ('Refresh failed',))
            ui.view.set_sessions([{'id': 'ws_one', 'name': 'Current'}])
            await ui.wait_render()
            self.assertNotIn('Stale', ui.screen_text())

    async def test_inspector_never_renders_native_id_and_keeps_exact_recovery_hints(self):
        async with tui_harness(size=(120, 30)) as ui:
            agent = {'agent_id': 'ag_one', 'registry_name': 'worker', 'provider': 'codex',
                     'native_session_id': 'SECRET-NATIVE-IDENTIFIER', 'last_state': 'exited',
                     'history_mode': 'literal', 'history_state': 'pending', 'cwd': None}
            ui.controller._select({'id': 'ws_one', 'name': 'Session', 'channel': 'general', 'agents': [agent]})
            ui.controller.data_dir = '/tmp/check'
            ui.controller._failed_launches.add('ag_one')
            ui.state.selected_agent_id = 'ag_one'
            ui.view.show_inspector()
            await ui.wait_render()
            screen = ui.screen_text()
            self.assertNotIn('SECRET-NATIVE-IDENTIFIER', screen)
            self.assertIn('id present', screen)
            self.assertIn('catching up…', screen)
            self.assertIn('⚠ cwd missing — /resume worker --cwd PATH', screen)
            self.assertIn('failed to start; see', screen)
            self.assertIn('/tmp/check/logs/wrapper-ag_one.log', screen)

    async def test_compact_agent_status_uses_one_row(self):
        async with tui_harness(size=(80, 18)) as ui:
            self.assertEqual(ui.view.agents_window.render_info.window_height, 1)
            self.assertGreaterEqual(ui.view.conversation_window.render_info.window_height, 8)

    async def test_nameless_workspace_title_falls_back_to_id(self):
        async with tui_harness() as ui:
            ui.controller._select({'id': 'ws_short', 'name': None, 'channel': 'general', 'agents': []})
            await ui.wait_render()
            self.assertIn('ws_short', ui.screen_text())
            self.assertNotIn('None ·', ui.screen_text())

    async def test_hidden_focus_request_does_not_corrupt_focus_name(self):
        async with tui_harness(size=(70, 16)) as ui:
            before = ui.state.focus_name
            self.assertFalse(ui.view.focus_named('navigation'))
            self.assertEqual(ui.state.focus_name, before)

    async def test_inspector_alt_sequence_does_not_close_or_leak(self):
        async with tui_harness() as ui:
            ui.controller._select({'id': 'ws_one', 'name': 'Session', 'channel': 'general',
                                   'agents': [{'agent_id': 'ag_one', 'last_state': 'exited'}]})
            ui.state.selected_agent_id = 'ag_one'
            ui.view.composer.text = 'draft'
            ui.view.show_inspector()
            await ui._send('\x1bx')
            self.assertTrue(ui.view.inspecting)
            self.assertEqual(ui.view.composer.text, 'draft')
            await ui.key('Escape')
            self.assertFalse(ui.view.inspecting)

    async def test_history_chunk_and_timestamp_updates_reorder_live_records(self):
        async with tui_harness() as ui:
            ui.client.handle_event({'type': 'message', 'data': {'id': 1, 'timestamp': 1, 'text': 'old-one'}})
            ui.client.handle_event({'type': 'message', 'data': {'id': 2, 'timestamp': 2, 'text': 'second'}})
            await ui.wait_render()
            ui.client.handle_event({'type': 'history', 'messages': [
                {'id': 1, 'timestamp': 3, 'text': 'chunk-one'}, {'id': 3, 'timestamp': 0, 'text': 'chunk-three'}]})
            await ui.wait_render()
            screen = ui.screen_text()
            self.assertIn('chunk-three', screen)
            self.assertLess(screen.index('chunk-three'), screen.index('second'))
            self.assertLess(screen.index('second'), screen.index('chunk-one'))
            ui.client.handle_event({'type': 'message_update', 'message': {
                'id': 2, 'timestamp': 4, 'text': 'updated-second'}})
            await ui.wait_render()
            screen = ui.screen_text()
            self.assertLess(screen.index('chunk-one'), screen.index('updated-second'))

    async def test_missing_cached_id_is_skipped_and_invalidates_order(self):
        async with tui_harness() as ui:
            ui.client.handle_event({'type': 'message', 'data': {'id': 1, 'text': 'gone'}})
            ui.client.handle_event({'type': 'message', 'data': {'id': 2, 'text': 'survivor'}})
            await ui.wait_render()
            # Defensive stale-reference probe intentionally bypasses normal model notifications.
            ui.client.messages.pop(1)
            content = ui.view.conversation.create_content(80, 10)
            self.assertNotIn('gone', ''.join(fragment[1] for line in content for fragment in line))
            self.assertIsNone(ui.view._transcript_key)
            ui.view.conversation.create_content(80, 10)
            self.assertEqual(ui.view._ordered_ids, (2,))

    async def test_activity_scroll_keys_and_alt_stay_scoped_across_resize(self):
        async with tui_harness() as ui:
            for ident in range(60):
                ui.client.show(f'activity-line-{ident}')
            ui.view.composer.text = 'preserved draft'
            ui.view.focus_named('navigation')
            await ui.key('F5')
            await ui._send('\x1bx')
            self.assertTrue(ui.view.activity_visible)
            self.assertEqual(ui.view.composer.text, 'preserved draft')
            await ui.key('PageDown')
            position = ui.view._activity_line
            self.assertGreater(position, 0)
            ui.view.focus_named('composer')
            await ui.key('PageDown')
            self.assertEqual(ui.view._activity_line, position)
            ui.view.focus_named('activity')
            await ui.resize(80, 24)
            await ui.key('Escape')
            self.assertFalse(ui.view.activity_visible)
            self.assertEqual(ui.focused_control, 'composer')
            self.assertEqual(ui.state.focus_name, 'composer')
            await ui.key('F5')
            await ui.resize(70, 16)
            self.assertFalse(ui.view.hide_activity())
            self.assertTrue(ui.view.activity_visible)
            await ui.resize(120, 30)
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'composer')
            self.assertEqual(ui.view.composer.text, 'preserved draft')

    async def test_activity_action_and_global_shortcuts_respect_open_modal(self):
        import asyncio
        async with tui_harness() as ui:
            self.assertIn('activity', {choice['id'] for choice in ui.view.action_choices()})
            task = asyncio.create_task(ui.dialogs.confirm('Keep modal open?', escape=False))
            try:
                await ui.wait_until(lambda: ui.dialogs.future is not None)
                for key in ('F2', 'F3', 'F4', 'F5'):
                    await ui.key(key)
                self.assertEqual(ui.calls, [])
                self.assertFalse(ui.view.activity_visible)
                await ui.key('F1')
                self.assertIn(('run_action', 'help', None), ui.calls)
                await ui.key('CtrlQ')
                self.assertIn(('quit',), ui.calls)
                self.assertFalse(task.done())
            finally:
                ui.dialogs.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_resize_helper_captures_exactly_one_render(self):
        async with tui_harness() as ui:
            for size in ((80, 24), (70, 16), (120, 30)):
                before = ui.render_count
                await ui.resize(*size)
                self.assertEqual(ui.render_count, before + 1)

    async def test_activity_restores_actual_focus_name_after_external_focus(self):
        async with tui_harness() as ui:
            ui.application.layout.focus(ui.view.navigation)
            ui.view.show_activity()
            await ui.key('Escape')
            self.assertEqual(ui.focused_control, 'navigation')
            self.assertEqual(ui.state.focus_name, 'navigation')


class ActivityComposerFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_close_keeps_current_composer_focus(self):
        async with tui_harness() as ui:
            ui.view.focus_named('navigation')
            await ui.key('F5')
            await ui.key('Tab')
            await ui.key('Tab')
            self.assertEqual(ui.focused_control, 'composer')
            await ui.paste('typing')
            await ui.key('F5')
            self.assertEqual(ui.focused_control, 'composer')
            self.assertEqual(ui.view.composer.text, 'typing')
