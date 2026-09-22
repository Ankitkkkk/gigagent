"""Composer modes and mention styling through real input and renderer cells."""

import asyncio
import unittest

from cli_view_contracts import SubmitOutcome
from tests._tui_harness import tui_harness


def bind(ui):
    ui.controller._select({'id': 'ws_one', 'name': 'One', 'channel': 'general', 'agents': []})

    async def submit(text):
        ui.calls.append(('sent', text))
        return SubmitOutcome('completed', sent=True)

    ui.bind_submit(submit)


class EditingTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_keeps_draft_safe_from_editing_shortcuts(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('keep')
            await ui.key('Home')
            await ui.key('Escape')
            await ui.key('CtrlD')
            await ui._send('\x0b')  # Ctrl+K must not erase the line in NORMAL.
            self.assertEqual(ui.view.composer.text, 'keep')
            await ui._send('i')
            await ui.key('CtrlD')
            self.assertEqual(ui.view.composer.text, 'eep')
            self.assertEqual(ui.calls, [])

    async def test_completion_enter_stays_insert_and_never_sends(self):
        async with tui_harness() as ui:
            bind(ui)
            ui.client.handle_event({'type': 'agents', 'data': ['agent-1', 'agent-2']})
            await ui._send('i@ag')
            await ui.wait_until(lambda: ui.view.composer.buffer.complete_state is not None)
            await ui.key('Tab')
            await ui.key('Enter')
            self.assertIn(ui.view.composer.text, ('@agent-1', '@agent-2'))
            self.assertEqual(ui.view.composer_mode, 'INSERT')
            await ui.key('Enter')
            self.assertTrue(ui.view.composer.text.endswith('\n'))
            self.assertEqual(ui.calls, [])

    async def test_normal_mode_does_not_intercept_dialog_answers(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('preserved draft')
            await ui.key('Escape')
            task = asyncio.create_task(ui.dialogs.confirm('Proceed?', default=False))
            await ui.wait_until(lambda: ui.dialogs.future is not None)
            await ui._send('y')
            self.assertTrue(await task)
            self.assertEqual(ui.view.composer_mode, 'NORMAL')
            self.assertEqual(ui.view.composer.text, 'preserved draft')
            self.assertEqual(ui.calls, [])

    async def test_normal_blocks_typing_and_insert_enter_cannot_send(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui._send('z')
            self.assertIn('NORMAL', ui.screen_text())
            self.assertEqual(ui.view.composer.text, '')
            await ui._send('ihello')
            self.assertIn('INSERT', ui.screen_text())
            await ui.key('Enter')
            await ui._send('world')
            self.assertEqual(ui.view.composer.text, 'hello\nworld')
            self.assertEqual(ui.calls, [])
            await ui.key('Escape')
            self.assertIn('NORMAL', ui.screen_text())
            await ui.key('Enter')
            await ui.wait_until(lambda: ui.view.composer.text == '')
            self.assertEqual(ui.calls, [('sent', 'hello\nworld')])

    async def test_paste_enters_insert_and_fast_escape_enter_sends(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('pasted\nmessage')
            self.assertIn('INSERT', ui.screen_text())
            await ui.key('Enter')
            self.assertEqual(ui.view.composer.text, 'pasted\nmessage\n')
            self.assertEqual(ui.calls, [])
            await ui._send('\x1b\r')
            await ui.wait_until(lambda: ui.view.composer.text == '')
            self.assertEqual(ui.calls, [('sent', 'pasted\nmessage\n')])

    async def test_normal_movement_and_I_A_edit_without_sending(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('hello world')
            await ui.key('Escape')
            await ui._send('0wl')
            self.assertEqual(ui.view.composer.buffer.cursor_position, 7)
            self.assertEqual(ui.view.composer.text, 'hello world')
            await ui._send('Istart ')
            await ui.key('Escape')
            await ui._send('A end')
            self.assertEqual(ui.view.composer.text, 'start hello world end')
            self.assertEqual(ui.calls, [])

    async def test_copy_mode_enter_cannot_send(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('keep draft')
            await ui.key('Escape')
            await ui._send('\x1b[18~')
            await ui.key('Enter')
            self.assertEqual(ui.view.composer.text, 'keep draft')
            self.assertEqual(ui.calls, [])
            await ui._send('\x1b[18~')
            await ui.key('Enter')
            await ui.wait_until(lambda: ui.view.composer.text == '')

    async def test_copy_return_from_transcript_resumes_typing_at_saved_cursor(self):
        for mode in ('INSERT', 'NORMAL'):
            with self.subTest(mode=mode):
                async with tui_harness() as ui:
                    bind(ui)
                    ui.client.handle_event({'type': 'message', 'data': {
                        'id': 1, 'sender': 'agent', 'text': 'copy this message'}})
                    await ui.paste('keep draft')
                    await ui.key('Left')
                    if mode == 'NORMAL':
                        await ui.key('Escape')
                    y, row = next((y, row) for y, row in enumerate(ui.rows)
                                  if 'copy this message' in row)
                    await ui.click(row.index('copy this message') + 1, y)
                    self.assertEqual(ui.focused_control, 'conversation')
                    await ui._send('\x1b[18~')
                    await ui._send('\x1b[18~')
                    self.assertEqual(ui.focused_control, 'composer')
                    self.assertEqual(ui.view.composer_mode, mode)
                    self.assertEqual(ui.view.composer.buffer.cursor_position, 9)
                    if mode == 'NORMAL':
                        await ui._send('i')
                    await ui._send('Z')
                    self.assertEqual(ui.view.composer.text, 'keep drafZt')
                    await ui.send_message()
                    await ui.wait_until(lambda: ui.view.composer.text == '')
                    self.assertEqual(ui.calls, [('sent', 'keep drafZt')])

    async def test_copy_keys_cannot_change_draft_or_editing_mode(self):
        for mode in ('INSERT', 'NORMAL'):
            with self.subTest(mode=mode):
                async with tui_harness() as ui:
                    bind(ui)
                    await ui.paste('keep draft')
                    if mode == 'NORMAL':
                        await ui.key('Escape')
                    await ui._send('\x1b[18~')
                    await ui._send('iZ')
                    await ui.paste('unwanted paste')
                    await ui.key('Escape')
                    await ui._send('\x1b\r')
                    await ui.key('CtrlD')
                    await ui.key('Enter')
                    self.assertEqual(ui.view.composer.text, 'keep draft')
                    self.assertEqual(ui.view.composer_mode, mode)
                    self.assertEqual(ui.view.composer.buffer.cursor_position, 10)
                    self.assertEqual(ui.calls, [])
                    await ui.key('Tab')
                    await ui._send('\x1b[18~')
                    self.assertEqual(ui.focused_control, 'composer')
                    self.assertEqual(ui.view.composer_mode, mode)
                    if mode == 'NORMAL':
                        await ui._send('i')
                    await ui._send('Z')
                    self.assertEqual(ui.view.composer.text, 'keep draftZ')

    async def test_copy_shortcuts_cannot_leave_input_selection_or_move_cursor(self):
        async with tui_harness() as ui:
            bind(ui)
            await ui.paste('keep draft')
            await ui.key('Left')
            await ui._send('\x1b[18~')
            await ui.key('CtrlSpace')
            await ui.key('Home')
            await ui.key('Left')
            await ui._send('\x1b[1;2H')  # Shift+Home must not select the draft.
            await ui._send('\x1b[18~')
            self.assertIsNone(ui.view.composer.buffer.selection_state)
            self.assertEqual(ui.view.composer.buffer.cursor_position, 9)
            await ui._send('Z')
            self.assertEqual(ui.view.composer.text, 'keep drafZt')

    async def test_mentions_are_bright_bold_without_changing_raw_draft(self):
        async with tui_harness() as ui:
            bind(ui)
            text = 'Ask @agent-1 and @codex-2\nmail user@example.com'
            await ui.paste(text)
            screen = ui.application.renderer.last_rendered_screen
            y, row = next((y, row) for y, row in enumerate(ui.rows) if 'Ask @agent-1' in row)
            for mention in ('@agent-1', '@codex-2'):
                start = row.index(mention)
                for x in range(start, start + len(mention)):
                    attrs = ui.view.style.get_attrs_for_style_str(screen.data_buffer[y][x].style)
                    self.assertTrue(attrs.bold)
                    self.assertEqual(attrs.color, 'ansibrightcyan')
            y, row = next((y, row) for y, row in enumerate(ui.rows) if 'user@example.com' in row)
            self.assertNotIn('composer.mention', screen.data_buffer[y][row.index('@')].style)
            self.assertEqual(ui.view.composer.text, text)
            self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), text)
