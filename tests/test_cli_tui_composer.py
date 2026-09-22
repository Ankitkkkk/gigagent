"""Composer regressions through the real pipe-input Application."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from cli_view_contracts import SubmitOutcome
from tests._tui_harness import tui_harness


def bind(ui, submit=None):
    ui.controller._select({'id': 'ws_one', 'name': 'One', 'channel': 'general', 'agents': []})
    ui.bind_submit(submit or AsyncMock(return_value=SubmitOutcome('failed')))


class ComposerTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_sgr_wheel_scrolls_wrapped_lines_and_resumes_follow(self):
        async with tui_harness(size=(80, 24)) as ui:
            bind(ui)
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 1, 'text': 'x' * 6000, 'sender': 'peer'}})
            await ui.wait_render()
            tail = ui.view.conversation._visible_start
            self.assertGreater(tail[1], 3)
            await ui._send('\x1b[<64;5;4M')
            self.assertFalse(ui.state.viewport.follow)
            self.assertEqual(ui.view.conversation._visible_start, (1, tail[1] - 3))
            self.assertEqual(ui.state.viewport.line_offset, tail[1] - 3)
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 2, 'text': 'new tail', 'sender': 'peer'}})
            await ui.wait_render()
            self.assertEqual(ui.state.viewport.new_ids, {2})
            await ui._send('\x1b[<65;5;4M')
            self.assertFalse(ui.state.viewport.follow)
            await ui._send('\x1b[<65;5;4M')
            self.assertTrue(ui.state.viewport.follow)
            self.assertFalse(ui.state.viewport.new_ids)
            self.assertIn('new tail', ui.screen_text())
            bottom = ui.view.conversation._visible_start
            await ui._send('\x1b[<65;5;4M')
            self.assertEqual(ui.view.conversation._visible_start, bottom)
            ui.state.viewport.anchor(1, ui.client.messages, line_offset=1)
            await ui.wait_render()
            await ui._send('\x1b[<64;5;4M')
            self.assertEqual(ui.view.conversation._visible_start, (1, 0))
            await ui._send('\x1b[<64;5;4M')
            self.assertEqual(ui.view.conversation._visible_start, (1, 0))
            self.assertFalse(ui.state.viewport.follow)

    async def test_conversation_sgr_wheel_crosses_records_and_deleted_anchor(self):
        async with tui_harness() as ui:
            bind(ui)
            ui.client.handle_event({'type': 'history', 'messages': [
                {'id': i, 'timestamp': i, 'text': f'message {i}', 'sender': 'peer'}
                for i in range(1, 41)]})
            ui.client.handle_event({'type': 'history_complete'})
            ui.state.viewport.anchor(10, ui.client.messages)
            await ui.wait_render()
            await ui._send('\x1b[<64;26;4M')
            # Header, body, and spacing make each short message three lines.
            self.assertEqual(ui.view.conversation._visible_start, (9, 0))
            await ui._send('\x1b[<65;26;4M')
            self.assertEqual(ui.view.conversation._visible_start, (10, 0))
            ui.client.handle_event({'type': 'delete', 'ids': [10]})
            await ui.wait_render()
            self.assertEqual(ui.view.conversation._visible_start, (11, 0))
            await ui._send('\x1b[<64;26;4M')
            # Header, body, and spacing make each short message three lines.
            self.assertEqual(ui.view.conversation._visible_start, (9, 0))
            await ui._send('\x1b[<65;26;4M')
            self.assertEqual(ui.view.conversation._visible_start, (11, 0))
            ui.view.focus_named('conversation')
            await ui.key('End')
            self.assertTrue(ui.state.viewport.follow)
            self.assertIn('message 40', ui.screen_text())

    async def test_cancel_scheduled_send_before_start_releases_reservation(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('keep')
            original = ui.application.create_background_task
            cancelled = []
            def cancel_before_start(coro):
                task = original(coro)
                task.cancel()
                cancelled.append(task)
                return task
            await ui.key('Escape')
            with patch.object(ui.application, 'create_background_task', side_effect=cancel_before_start):
                # Escape must precede this mock: prompt-toolkit also schedules
                # terminal escape decoding through create_background_task().
                await ui.key('Enter')
            self.assertTrue(cancelled)
            await asyncio.gather(*cancelled, return_exceptions=True)
            await ui.wait_render()
            self.assertFalse(ui.composer_actions.sending)
            self.assertEqual(ui.view.composer.text, 'keep')
            await ui.key('Enter')
            await ui.wait_until(lambda: ui.submit_mock.await_count == 1)

    async def test_failed_send_keeps_multiline_draft(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('one\ntwo')
            self.assertEqual(ui.view.composer.text, 'one\ntwo')
            await ui.send_message()
            self.assertEqual(ui.view.composer.text, 'one\ntwo')
            await ui.wait_until(lambda: ui.submit_mock.await_count == 1)

    async def test_ctrl_c_without_overlay_preserves_composer_text(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('keep\nthis')
            await ui.key('CtrlC')
            self.assertEqual(ui.view.composer.text, 'keep\nthis')

    async def test_ctrl_c_closes_overlay_preserving_composer_text(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('keep this')
            task = asyncio.create_task(ui.dialogs.confirm('Clear?', default=False))
            await ui.wait_until(lambda: ui.dialogs.future is not None)
            await ui.key('CtrlC')
            self.assertFalse(await task)
            self.assertEqual(ui.view.composer.text, 'keep this')

    async def test_fast_escape_enter_sends_then_paste_and_ctrl_d_edit(self):
        async with tui_harness() as ui:
            bind(ui, AsyncMock(return_value=SubmitOutcome('completed')))
            await ui.paste('one')
            await ui.key('AltEnter')
            await ui.wait_until(lambda: ui.view.composer.text == '')
            self.assertEqual(ui.submit_mock.await_args.args, ('one',))
            await ui.paste('two')
            self.assertEqual(ui.view.composer.text, 'two')
            self.assertEqual(ui.submit_mock.await_count, 1)
            await ui.key('Home')
            await ui.key('CtrlD')
            self.assertEqual(ui.view.composer.text, 'wo')
            await ui.send_message()
            await ui.wait_until(lambda: ui.view.composer.text == '')
            self.assertEqual(ui.submit_mock.await_count, 2)
            await ui.key('CtrlD')
            await ui.wait_until(lambda: ('quit',) in ui.calls)

    async def test_composer_grows_and_shrinks_with_cursor_preserved(self):
        async with tui_harness() as ui:
            bind(ui, AsyncMock(return_value=SubmitOutcome('completed')))
            await ui.paste('1\n2\n3\n4\n5\n6\n7')
            self.assertEqual(ui.view.composer.window.render_info.window_height, 6)
            self.assertEqual(ui.view.composer.buffer.cursor_position, 13)
            self.assertIn('7', ui.screen_text())
            await ui.send_message()
            await ui.wait_until(lambda: ui.view.composer.text == '')
            self.assertEqual(ui.view.composer.window.render_info.window_height, 3)

    async def test_draft_switch_preserves_raw_text_cursor_and_limits(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('raw\x1b[31m\n界')
            await ui.key('Left')
            before = (ui.view.composer.text, ui.view.composer.buffer.cursor_position)
            actions = ui.composer_actions
            self.assertTrue(actions.switch_draft(('session', 'ws_two')))
            await ui.paste('second')
            self.assertTrue(actions.switch_draft(('session', 'ws_one')))
            self.assertEqual((ui.view.composer.text, ui.view.composer.buffer.cursor_position), before)
            await ui.paste('界' * 21846)
            self.assertEqual((ui.view.composer.text, ui.view.composer.buffer.cursor_position), before)
            self.assertIn('64 KiB', ui.screen_text())
            for index in range(48):
                self.assertTrue(ui.state.drafts.set(('session', str(index)), 'saved'))
            self.assertFalse(actions.switch_draft(('session', 'blocked')))
            self.assertEqual(actions.key, ('session', 'ws_one'))
            self.assertEqual(ui.state.notices.lines[-1], '50 unsent drafts; send or clear one')
            self.assertTrue(actions.switch_draft(None, mandatory=True))
            self.assertEqual(ui.view.composer.text, '')
            await ui.paste('inactive')
            self.assertEqual(ui.view.composer.text, '')
            self.assertNotIn(None, dict(ui.state.drafts.items()))
            self.assertTrue(actions.switch_draft(('session', 'new'), mandatory=True))
            await ui.paste('cannot exceed count')
            self.assertEqual(ui.view.composer.text, '')
            self.assertEqual(len(ui.state.drafts), 50)

    async def test_inactive_composer_blocks_edit_and_send(self):
        async with tui_harness() as ui:
            ui.bind_submit(AsyncMock(return_value=SubmitOutcome('completed')))
            await ui.paste('no destination')
            await ui.key('AltEnter')
            await ui.send_message()
            self.assertEqual(ui.view.composer.text, '')
            self.assertEqual(ui.submit_mock.await_count, 0)

    async def test_pending_send_keeps_new_edits_and_blocks_duplicate_activation(self):
        async with tui_harness() as ui:
            gate = asyncio.Future()
            async def submit(text):
                ui.calls.append(('sent_snapshot', text))
                return await gate
            bind(ui, submit)
            await ui.paste('earlier')
            await ui.send_message()
            await ui.wait_until(lambda: ('sent_snapshot', 'earlier') in ui.calls)
            await ui.paste(' newer')
            await ui.send_message()
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 1, 'channel': 'general', 'text': 'live arrival', 'sender': 'peer'}})
            await ui.wait_render()
            self.assertIn('live arrival', ui.screen_text())
            self.assertEqual(ui.view.composer.text, 'earlier newer')
            gate.set_result(SubmitOutcome('completed', sent=True))
            await ui.wait_until(lambda: not ui.composer_actions.sending)
            self.assertEqual(ui.view.composer.text, 'earlier newer')
            self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'earlier newer')
            self.assertEqual(ui.calls.count(('sent_snapshot', 'earlier')), 1)
            self.assertIn('earlier message was sent', ui.state.notices.lines[-1])
            self.assertEqual(list(ui.client.messages), [1])

    async def test_send_does_not_clear_another_selected_draft(self):
        async with tui_harness() as ui:
            gate = asyncio.Future()
            async def submit(text):
                return await gate
            bind(ui, submit)
            await ui.paste('first')
            await ui.send_message()
            ui.controller._select({'id': 'second', 'name': 'Second', 'channel': 'second-channel', 'agents': []})
            self.assertTrue(ui.composer_actions.switch_draft(('session', 'second')))
            await ui.paste('second')
            gate.set_result(SubmitOutcome('completed', sent=True))
            await ui.wait_until(lambda: not ui.composer_actions.sending)
            self.assertEqual(ui.view.composer.text, 'second')
            self.assertEqual(ui.state.drafts.get(('session', 'second')), 'second')
            self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), '')

    async def test_successful_command_dialog_respects_newer_edits(self):
        async with tui_harness() as ui:
            async def submit(text):
                await ui.dialogs.confirm('Proceed?', default=False)
                return SubmitOutcome('completed')
            bind(ui, submit)
            await ui.paste('/rename old')
            await ui.send_message()
            await ui.wait_until(lambda: ui.dialogs.future is not None)
            # Simulate an external draft update while the command awaits its dialog.
            from prompt_toolkit.document import Document
            text = ui.view.composer.text + ' newer'
            ui.view.composer.buffer.set_document(Document(text, len(text)), bypass_readonly=True)
            await ui._send('y')
            await ui.wait_until(lambda: not ui.composer_actions.sending)
            self.assertEqual(ui.view.composer.text, '/rename old newer')

    async def test_clear_draft_confirmation_yes_no_and_escape(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('valuable')
            for key in ('n', 'Escape', 'y'):
                task = asyncio.create_task(ui.composer_actions.clear_draft())
                await ui.wait_until(lambda: ui.dialogs.future is not None)
                if key == 'Escape':
                    await ui.key(key)
                else:
                    await ui._send(key)
                await task
                self.assertEqual(ui.view.composer.text, '' if key == 'y' else 'valuable')
            self.assertEqual(len(ui.state.drafts), 0)

    async def test_completion_enter_tab_and_ctrl_c(self):
        async with tui_harness() as ui:
            bind(ui)
            ui.client.handle_event({'type': 'agents', 'data': ['claude-2', 'claude-3']})
            await ui._send('i')
            await ui._send('@cl')
            await ui.wait_until(lambda: ui.view.composer.buffer.complete_state is not None)
            self.assertIn('claude-2', ui.screen_text())
            await ui.key('Tab')
            self.assertEqual(ui.focused_control, 'composer')
            await ui.key('Enter')
            self.assertEqual(ui.view.composer.text, '@claude-2')
            self.assertEqual(ui.submit_mock.await_count, 0)
            await ui.key('Tab')
            self.assertEqual(ui.focused_control, 'clear_draft')
            await ui.key('Tab')
            self.assertEqual(ui.focused_control, 'navigation')
            ui.view.focus_named('composer')
            await ui._send(' @cl')
            await ui.wait_until(lambda: ui.view.composer.buffer.complete_state is not None)
            text = ui.view.composer.text
            await ui.key('CtrlC')
            self.assertIsNone(ui.view.composer.buffer.complete_state)
            self.assertEqual(ui.view.composer.text, text)

    async def test_completion_context_and_sanitized_metadata(self):
        from prompt_toolkit.document import Document
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.formatted_text import to_formatted_text
        async with tui_harness() as ui:
            bind(ui)
            ui.controller.providers = ['kilo', 'bad\x1b[31m']
            ui.controller.workspace['agents'] = [{'agent_id': 'ag_full_id', 'registry_name': 'kilo-2'}]
            ui.client.channels = ['general', 'dev']
            def choices(text):
                return list(ui.view.composer.buffer.completer.get_completions(Document(text), CompleteEvent()))
            self.assertEqual([c.text for c in choices('/spawn k')], ['kilo'])
            self.assertEqual([c.text for c in choices('/stop k')], ['kilo-2'])
            self.assertEqual([c.text for c in choices('/history kilo-2 n')], ['none'])
            self.assertEqual(choices('/join '), [])
            self.assertTrue(all(c.display_meta for c in choices('/')))
            bad = choices('/spawn bad')[0]
            self.assertEqual(bad.text, 'bad\x1b[31m')
            self.assertNotIn('\x1b', str(to_formatted_text(bad.display)))
            ui.controller.plain_channel = True
            self.assertEqual(choices('/spawn '), [])
            self.assertEqual([c.text for c in choices('/join d')], ['dev'])
            self.assertNotIn('/spawn', [c.text for c in choices('/')])

    async def test_escape_enter_is_scoped_away_from_dialog_and_hidden_composer(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('keep')
            task = asyncio.create_task(ui.dialogs.confirm('Keep?', default=False))
            await ui.wait_until(lambda: ui.dialogs.future is not None)
            await ui.key('AltEnter')
            self.assertFalse(task.done())
            self.assertEqual(ui.view.composer.text, 'keep')
            await ui.key('Escape')
            await task
            await ui.resize(70, 16)
            await ui.key('AltEnter')
            self.assertEqual(ui.view.composer.text, 'keep')
            await ui.key('F1')
            await ui.wait_until(lambda: ('run_action', 'help', None) in ui.calls)
            await ui.key('CtrlQ')
            await ui.wait_until(lambda: ('quit',) in ui.calls)

    async def test_page_keys_anchor_live_messages_and_click_follows(self):
        async with tui_harness() as ui:
            bind(ui)
            ui.client.handle_event({'type': 'history', 'messages': [
                {'id': i, 'timestamp': i, 'channel': 'general', 'text': f'message {i}', 'sender': 'peer'}
                for i in range(1, 41)]})
            ui.client.handle_event({'type': 'history_complete'})
            await ui.wait_render()
            await ui.paste('typing')
            await ui.key('PageUp')
            self.assertTrue(ui.state.viewport.follow)
            ui.view.focus_named('conversation')
            await ui.key('PageUp')
            self.assertFalse(ui.state.viewport.follow)
            anchor = ui.state.viewport.anchor_id
            before = ui.view.conversation.text()
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 41, 'timestamp': 41, 'text': 'new unseen', 'sender': 'peer'}})
            await ui.wait_render()
            self.assertEqual(ui.state.viewport.anchor_id, anchor)
            self.assertEqual(ui.view.conversation.text(), before)
            self.assertEqual(ui.state.viewport.new_ids, {41})
            ui.client.handle_event({'type': 'message_update', 'message': {
                'id': 41, 'timestamp': 41, 'text': 'updated unseen', 'sender': 'peer'}})
            ui.client.handle_event({'type': 'history', 'messages': [{'id': 42, 'timestamp': 42, 'text': 'history'}]})
            ui.client.handle_event({'type': 'history_complete'})
            self.assertEqual(ui.state.viewport.new_ids, {41})
            await ui.key('PageDown')
            self.assertGreater(ui.state.viewport.anchor_id, anchor)
            await ui.key('End')
            self.assertTrue(ui.state.viewport.follow)
            self.assertFalse(ui.state.viewport.new_ids)
            await ui.key('PageUp')
            await ui.click(25, 3)
            self.assertTrue(ui.state.viewport.follow)
            self.assertEqual(ui.view.composer.text, 'typing')
            await ui.key('F5')
            await ui.key('PageUp')
            self.assertTrue(ui.state.viewport.follow)

    async def test_real_disconnected_submit_keeps_text_without_replay(self):
        async with tui_harness() as ui:
            bind(ui, ui.client.submit_outcome)
            await ui.paste('offline message')
            await ui.send_message()
            await ui.wait_until(lambda: not ui.composer_actions.sending)
            self.assertEqual(ui.view.composer.text, 'offline message')
            self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'offline message')
            self.assertEqual(len(ui.state.notices.lines), 1)
            ui.client._set_connection_state('connected')
            await ui.wait_render()
            self.assertEqual(ui.view.composer.text, 'offline message')
            self.assertEqual(ui.client.messages, {})

    async def test_channel_rename_moves_cursor_at_capacity_and_collision_retains_both(self):
        from cli_tui_state import DraftStore
        store = DraftStore()
        for index in range(50):
            store.set(('channel', str(index)), 'draft' + str(index), cursor=2)
        self.assertTrue(store.rename(('channel', '0'), ('channel', 'renamed')))
        self.assertEqual(len(store), 50)
        self.assertEqual(store.get(('channel', 'renamed')), 'draft0')
        self.assertEqual(store.get_cursor(('channel', 'renamed')), 2)
        self.assertEqual(store.get(('channel', '0')), '')
        self.assertFalse(store.rename(('channel', '1'), ('channel', 'renamed')))
        self.assertEqual(store.get(('channel', '1')), 'draft1')
        self.assertEqual(store.get(('channel', 'renamed')), 'draft0')
        async with tui_harness() as ui:
            ui.controller.plain_channel = True
            ui.bind_submit(AsyncMock(return_value=SubmitOutcome('completed', sent=True)))
            await ui.paste('active text')
            await ui.key('Left')
            ui.client.channel = 'renamed'
            self.assertTrue(ui.composer_actions.rename_channel('general', 'renamed'))
            self.assertEqual(ui.composer_actions.key, ('channel', 'renamed'))
            self.assertEqual(ui.view.composer.buffer.cursor_position, 10)
            ui.state.drafts.set(('channel', 'collision'), 'other text', cursor=3)
            ui.client.channel = 'collision'
            self.assertFalse(ui.composer_actions.rename_channel('renamed', 'collision'))
            self.assertEqual(ui.composer_actions.key, ('channel', 'renamed'))
            self.assertEqual(ui.view.composer.text, 'active text')
            self.assertEqual(ui.state.drafts.get(('channel', 'collision')), 'other text')
            await ui.send_message()
            self.assertEqual(ui.submit_mock.await_count, 0)
            self.assertEqual(ui.view.composer.text, 'active text')
            self.assertIn('destination changed', ui.state.notices.lines[-1])
            self.assertTrue(ui.composer_actions.switch_draft(('channel', 'collision')))
            self.assertEqual(ui.view.composer.text, 'other text')
            self.assertEqual(ui.view.composer.buffer.cursor_position, 3)
            self.assertTrue(ui.composer_actions.rename_channel('renamed', 'inactive'))
            self.assertEqual(ui.composer_actions.key, ('channel', 'collision'))
            self.assertEqual(ui.state.drafts.get(('channel', 'inactive')), 'active text')

    async def test_keep_running_false_routes_quit_without_duplicate_notice(self):
        async with tui_harness() as ui:
            async def submit(text):
                ui.notice('Already published')
                return SubmitOutcome('completed', keep_running=False, message='Already published')
            bind(ui, submit)
            await ui.paste('/quit')
            await ui.send_message()
            await ui.wait_until(lambda: ('quit',) in ui.calls)
            self.assertEqual(ui.view.composer.text, '')
            self.assertEqual(ui.state.notices.lines, ('Already published',))

    async def test_channel_change_before_enter_refuses_stale_destination(self):
        async with tui_harness() as ui:
            ui.controller.plain_channel = True
            ui.bind_submit(AsyncMock(return_value=SubmitOutcome('completed', sent=True)))
            ui.client.handle_event({'type': 'settings', 'data': {'channels': ['general', 'dev']}})
            await ui.paste('must stay here')
            await ui.key('Left')
            before = (ui.view.composer.text, ui.view.composer.buffer.cursor_position)
            await ui.client.submit_outcome('/join dev')
            await ui.send_message()
            self.assertEqual(ui.client.channel, 'dev')
            self.assertEqual(ui.submit_mock.await_count, 0)
            self.assertEqual((ui.view.composer.text, ui.view.composer.buffer.cursor_position), before)
            self.assertIn('destination changed', ui.screen_text())

    async def test_page_keys_scroll_inside_single_long_message(self):
        async with tui_harness() as ui:
            bind(ui)
            ui.client.handle_event({'type': 'message', 'data': {
                'id': 1, 'text': '\n'.join(f'line {i:03}' for i in range(100)), 'sender': 'peer'}})
            await ui.wait_render()
            self.assertIn('line 099', ui.screen_text())
            ui.view.focus_named('conversation')
            await ui.key('PageUp')
            screen = ui.screen_text()
            self.assertNotIn('line 099', screen)
            self.assertNotIn('line 000', screen)
            await ui.key('PageUp')
            self.assertNotEqual(screen, ui.screen_text())
            await ui.key('PageDown')
            self.assertEqual(screen, ui.screen_text())
            await ui.key('End')
            self.assertIn('line 099', ui.screen_text())

    async def test_ctrl_c_closes_read_only_overlays_preserving_draft(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('keep cursor')
            await ui.key('Left')
            before = (ui.view.composer.text, ui.view.composer.buffer.cursor_position)
            await ui.key('F5')
            await ui.key('CtrlC')
            self.assertFalse(ui.view.activity_visible)
            ui.controller.workspace['agents'] = [{'agent_id': 'ag_one', 'last_state': 'exited'}]
            ui.state.selected_agent_id = 'ag_one'
            ui.view.show_inspector()
            await ui.key('CtrlC')
            self.assertFalse(ui.view.inspecting)
            self.assertEqual((ui.view.composer.text, ui.view.composer.buffer.cursor_position), before)

    async def test_clear_confirmation_keeps_newer_text_and_cancelled_send(self):
        async with tui_harness() as ui:
            bind(ui, AsyncMock(return_value=SubmitOutcome('cancelled')))
            await ui.paste('cancelled draft')
            await ui.send_message()
            self.assertEqual(ui.view.composer.text, 'cancelled draft')
            task = asyncio.create_task(ui.composer_actions.clear_draft())
            await ui.wait_until(lambda: ui.dialogs.future is not None)
            from prompt_toolkit.document import Document
            text = ui.view.composer.text + ' changed'
            ui.view.composer.buffer.set_document(Document(text, len(text)), bypass_readonly=True)
            await ui._send('y')
            self.assertFalse(await task)
            self.assertEqual(ui.view.composer.text, 'cancelled draft changed')

    async def test_composer_growth_at_compact_minimum_preserves_single_render_resize(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('界' * 100)
            await ui.resize(80, 18)
            self.assertEqual(ui.view.composer.window.render_info.window_height, 3)
            await ui.paste('\n4\n5\n6\n7')
            self.assertEqual(ui.view.composer.window.render_info.window_height, 6)
            self.assertIn('7', ui.screen_text())
            before = (ui.view.composer.text, ui.view.composer.buffer.cursor_position)
            await ui.resize(70, 16)
            await ui.resize(80, 18)
            self.assertEqual((ui.view.composer.text, ui.view.composer.buffer.cursor_position), before)
            self.assertEqual(ui.view.composer.window.render_info.window_height, 6)
            self.assertIn('Ctrl+Q Quit', ui.screen_text())

    async def test_successful_join_clears_only_original_command_draft(self):
        async with tui_harness() as ui:
            ui.controller.plain_channel = True
            ui.client.channels = ['general', 'dev']
            ui.state.drafts.set(('channel', 'dev'), 'waiting in dev', cursor=4)
            async def submit(text):
                result = await ui.client.submit_outcome(text)
                ui.composer_actions.switch_draft(ui.composer_actions.destination_key())
                return result
            ui.bind_submit(submit)
            await ui.paste('/join dev')
            await ui.send_message()
            await ui.wait_until(lambda: not ui.composer_actions.sending)
            self.assertEqual(ui.client.channel, 'dev')
            self.assertEqual(ui.view.composer.text, 'waiting in dev')
            self.assertEqual(ui.view.composer.buffer.cursor_position, 4)
            self.assertEqual(ui.state.drafts.get(('channel', 'general')), '')

    async def test_send_keeps_switched_back_edited_and_clear_recreated_drafts(self):
        for replacement in ('first edited', 'first'):
            with self.subTest(replacement=replacement):
                async with tui_harness() as ui:
                    gate = asyncio.Future()
                    async def submit(text):
                        return await gate
                    bind(ui, submit)
                    await ui.paste('first')
                    await ui.send_message()
                    ui.controller._select({'id': 'second', 'name': 'Second', 'channel': 'second', 'agents': []})
                    ui.composer_actions.switch_draft(('session', 'second'))
                    await ui.paste('second')
                    ui.controller._select({'id': 'ws_one', 'name': 'One', 'channel': 'general', 'agents': []})
                    ui.composer_actions.switch_draft(('session', 'ws_one'))
                    task = asyncio.create_task(ui.composer_actions.clear_draft())
                    await ui.wait_until(lambda: ui.dialogs.future is not None)
                    await ui._send('y')
                    self.assertTrue(await task)
                    await ui.paste(replacement)
                    gate.set_result(SubmitOutcome('completed', sent=True))
                    await ui.wait_until(lambda: not ui.composer_actions.sending)
                    self.assertEqual(ui.view.composer.text, replacement)
                    self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), replacement)

    async def test_draft_revision_ignores_cursor_but_detects_aba_and_rename(self):
        from cli_tui_state import DraftStore
        drafts = DraftStore()
        drafts.set(('channel', 'old'), 'same', cursor=2)
        version = drafts.revision(('channel', 'old'))
        drafts.set_cursor(('channel', 'old'), 3)
        drafts.set(('channel', 'old'), 'same', cursor=1)
        self.assertEqual(drafts.revision(('channel', 'old')), version)
        drafts.clear(('channel', 'old'))
        drafts.set(('channel', 'old'), 'same')
        self.assertNotEqual(drafts.revision(('channel', 'old')), version)
        version = drafts.revision(('channel', 'old'))
        drafts.rename(('channel', 'old'), ('channel', 'new'))
        drafts.rename(('channel', 'new'), ('channel', 'old'))
        self.assertNotEqual(drafts.revision(('channel', 'old')), version)
        self.assertIsNone(drafts.revision(('channel', 'new')))

    async def test_empty_active_channel_rename_never_overwrites_destination_draft(self):
        async with tui_harness() as ui:
            ui.controller.plain_channel = True
            ui.bind_submit(AsyncMock(return_value=SubmitOutcome('failed')))
            ui.state.drafts.set(('channel', 'dev'), 'saved destination', cursor=4)
            self.assertFalse(ui.composer_actions.rename_channel('general', 'dev'))
            self.assertEqual(ui.composer_actions.key, ('channel', 'general'))
            await ui.paste('source draft')
            self.assertEqual(ui.state.drafts.get(('channel', 'dev')), 'saved destination')
            self.assertEqual(ui.state.drafts.get_cursor(('channel', 'dev')), 4)

    async def test_completion_menu_is_hidden_in_resize_mode(self):
        async with tui_harness() as ui:
            bind(ui)
            ui.client.handle_event({'type': 'agents', 'data': ['claude-2', 'claude-3']})
            await ui._send('i')
            await ui._send('@cl')
            await ui.wait_until(lambda: ui.view.composer.buffer.complete_state is not None)
            await ui.resize(70, 16)
            self.assertNotIn('claude-2', ui.screen_text())
            self.assertIn('Resize terminal', ui.screen_text())
            await ui.key('CtrlSpace')
            self.assertNotIn('claude-2', ui.screen_text())
            await ui.resize(120, 30)
            self.assertEqual(ui.view.composer.text, '@cl')

    async def test_enter_sends_exact_mention_and_channel_without_selecting_prefix(self):
        for text in ('hi @claude', 'hi #dev'):
            with self.subTest(text=text):
                async with tui_harness() as ui:
                    bind(ui, AsyncMock(return_value=SubmitOutcome('completed', sent=True)))
                    ui.client.handle_event({'type': 'agents', 'data': ['claude-2', 'claude']})
                    ui.client.channels = ['dev-ops', 'dev']
                    await ui._send('i')
                    await ui._send(text)
                    await ui.wait_until(lambda: ui.view.composer.buffer.complete_state is not None)
                    self.assertIsNone(ui.view.composer.buffer.complete_state.complete_index)
                    await ui.send_message()
                    self.assertEqual(ui.submit_mock.await_count, 1)
                    self.assertEqual(ui.submit_mock.await_args.args, (text,))
                    self.assertEqual(ui.view.composer.text, '')

    async def test_delayed_escape_enter_sends_after_escape_timeout(self):
        async with tui_harness() as ui:
            bind(ui, AsyncMock(return_value=SubmitOutcome('completed', sent=True)))
            await ui.paste('partial')
            await asyncio.to_thread(ui.pipe.send_text, '\x1b')
            # Deliberately exceed VT decoding timeout, but stay inside key sequence timeout.
            await asyncio.sleep(0.2)
            await ui.key('Enter')
            await ui.wait_until(lambda: ui.view.composer.text == '')
            self.assertEqual(ui.submit_mock.await_count, 1)
            self.assertEqual(ui.submit_mock.await_args.args, ('partial',))

    async def test_enter_typeahead_uses_original_snapshot_and_claims_send_synchronously(self):
        for status in ('completed', 'failed'):
            with self.subTest(status=status):
                async with tui_harness() as ui:
                    bind(ui, AsyncMock(return_value=SubmitOutcome(status, sent=status == 'completed')))
                    await ui.paste('hello')
                    await ui._send('\x1b\r\rixyz')
                    await ui.wait_until(lambda: not ui.composer_actions.sending)
                    self.assertEqual(ui.submit_mock.await_count, 1)
                    self.assertEqual(ui.submit_mock.await_args.args, ('hello',))
                    self.assertEqual(ui.view.composer.text, 'helloxyz')
                    self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'helloxyz')
                    self.assertEqual(ui.view.composer.buffer.cursor_position, 8)
                    if status == 'completed':
                        self.assertIn('earlier message was sent', ui.screen_text())
                    else:
                        self.assertEqual(ui.state.notices.lines, ())

    async def test_rename_into_active_empty_destination_reloads_moved_text_and_cursor(self):
        async with tui_harness() as ui:
            ui.controller.plain_channel = True
            ui.bind_submit(AsyncMock(return_value=SubmitOutcome('failed')))
            ui.state.drafts.set(('channel', 'old'), 'precious', cursor=3)
            self.assertTrue(ui.composer_actions.rename_channel('old', 'general'))
            self.assertEqual(ui.view.composer.text, 'precious')
            self.assertEqual(ui.view.composer.buffer.cursor_position, 3)
            await ui.paste('x')
            self.assertEqual(ui.state.drafts.get(('channel', 'general')), 'prexcious')
            ui.state.drafts.set(('channel', 'another'), 'another draft', cursor=5)
            self.assertFalse(ui.composer_actions.rename_channel('another', 'general'))
            self.assertEqual(ui.view.composer.text, 'prexcious')
            self.assertEqual(ui.state.drafts.get(('channel', 'another')), 'another draft')
            self.assertEqual(ui.state.drafts.get_cursor(('channel', 'another')), 5)

    async def test_completer_uses_canonical_legacy_commands_and_descriptions(self):
        from prompt_toolkit.document import Document
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.formatted_text import fragment_list_to_text
        from cli_workspace_chat import SESSION_COMMANDS
        async with tui_harness() as ui:
            bind(ui)
            def choices():
                return {c.text: fragment_list_to_text(c.display_meta) for c in
                        ui.view.composer.buffer.completer.get_completions(Document('/'), CompleteEvent())}
            self.assertTrue(SESSION_COMMANDS.issubset(choices()))
            ui.controller.plain_channel = True
            commands = choices()
            self.assertIn('/continue', commands)
            self.assertIn('/summary', commands)
            self.assertEqual(commands['/agents'], 'Show agent availability and roles')
            self.assertEqual(commands['/join'], 'Switch to an existing channel')

    async def test_rejected_keystrokes_coalesce_until_accepted_edit_or_destination_change(self):
        async with tui_harness() as ui:
            bind(ui)
            for index in range(50):
                ui.state.drafts.set(('session', str(index)), 'saved')
            await ui._send('abc')
            self.assertEqual(ui.view.composer.text, '')
            self.assertEqual(ui.state.notices.lines, ('50 unsent drafts; send or clear one',))
            ui.state.drafts.clear(('session', '0'))
            await ui._send('accepted')
            ui.composer_actions.switch_draft(('session', 'next'), mandatory=True)
            await ui._send('xyz')
            self.assertEqual(ui.state.notices.lines, ('50 unsent drafts; send or clear one',) * 2)

    async def test_bypass_edit_without_destination_uses_inactive_notice(self):
        from prompt_toolkit.document import Document
        async with tui_harness() as ui:
            ui.bind_submit(AsyncMock(return_value=SubmitOutcome('failed')))
            for text in ('attempt', 'another'):
                ui.view.composer.buffer.set_document(Document(text), bypass_readonly=True)
            await ui.wait_render()
            self.assertEqual(ui.view.composer.text, '')
            self.assertEqual(len(ui.state.drafts), 0)
            self.assertEqual(ui.state.notices.lines, ('Select a destination before editing',))

    async def test_completer_skips_malformed_agent_records(self):
        from prompt_toolkit.document import Document
        from prompt_toolkit.completion import CompleteEvent
        async with tui_harness() as ui:
            bind(ui)
            # Both renderer and completer tolerate malformed records.
            ui.controller.workspace['agents'] = [{}, None, {'registry_name': 'missing-id'},
                {'agent_id': 'ag_valid', 'registry_name': 'claude-2'}, {'agent_id': ''}]
            try:
                result = list(ui.view.composer.buffer.completer.get_completions(
                    Document('/stop '), CompleteEvent()))
                self.assertEqual([c.text for c in result], ['claude-2'])
                await ui.wait_render()
                self.assertIn('claude-2', ui.screen_text())
            finally:
                ui.controller.workspace['agents'] = []

    async def test_single_enter_and_typeahead_keep_full_edited_draft(self):
        async with tui_harness() as ui:
            bind(ui, AsyncMock(return_value=SubmitOutcome('completed', sent=True)))
            await ui.paste('hello')
            await ui._send('\x1b\rixyz')
            self.assertEqual(ui.submit_mock.await_count, 1)
            self.assertEqual(ui.submit_mock.await_args.args, ('hello',))
            self.assertEqual(ui.view.composer.text, 'helloxyz')
            self.assertEqual(ui.view.composer.buffer.cursor_position, 8)
            self.assertIn('earlier message was sent', ui.screen_text())
