"""Real keyboard regressions for awaitable terminal dialogs."""

import asyncio
from contextlib import asynccontextmanager, suppress
import unittest

from prompt_toolkit.application import Application
from prompt_toolkit.filters import has_focus
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer, Float,
                                   FloatContainer, HSplit, Layout)
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea

from cli_tui_dialogs import DialogHost, Field, ModalResult


class DialogTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def dialog_application(self):
        installed = asyncio.Event()
        started = asyncio.Event()
        composer = TextArea(text='draft')
        app = None

        def invalidate():
            app.invalidate()
            if host.future is not None:
                installed.set()

        host = DialogHost(lambda: app, invalidate)
        self.installed = installed
        self.composer = composer
        keys = KeyBindings()

        @keys.add('escape', 'enter', filter=has_focus(composer))
        def composer_newline(event):
            event.current_buffer.insert_text('\n')

        root = FloatContainer(
            content=composer,
            floats=[Float(content=DynamicContainer(lambda: host.body))],
        )
        with create_pipe_input() as pipe:
            app = Application(layout=Layout(root, focused_element=composer),
                              key_bindings=keys, input=pipe, output=DummyOutput(),
                              full_screen=True)
            app.timeoutlen = 1.0
            app.ttimeoutlen = 0.01
            self.app = app
            running = asyncio.create_task(app.run_async(pre_run=started.set))
            self.answers = []
            try:
                await asyncio.wait_for(started.wait(), 1)
                yield host, pipe
            finally:
                for answer in self.answers:
                    if not answer.done():
                        answer.cancel()
                    with suppress(asyncio.CancelledError):
                        await answer
                running.cancel()
                with suppress(asyncio.CancelledError):
                    await running

    def request(self, coroutine):
        answer = asyncio.create_task(coroutine)
        self.answers.append(answer)
        return answer

    async def wait_modal(self, host):
        await asyncio.wait_for(self.installed.wait(), 1)
        self.installed.clear()
        self.assertIsNotNone(host.future)
        # Ensure prompt-toolkit has refreshed its parent-container mapping.
        await asyncio.sleep(0.03)

    async def result(self, answer):
        return await asyncio.wait_for(asyncio.shield(answer), 0.5)

    async def test_confirmation_y_n_default_and_escape(self):
        async with self.dialog_application() as (host, pipe):
            for keys, default, expected in [('y', False, True), ('n', True, False),
                                             ('\r', True, True), ('\r', False, False),
                                             ('\x1b', True, False)]:
                with self.subTest(keys=keys, default=default):
                    answer = self.request(host.confirm('Unarchive it? [y/N]',
                                                       default=default))
                    await self.wait_modal(host)
                    pipe.send_text(keys)
                    self.assertIs(await self.result(answer), expected)
                    self.assertIsNone(host.future)
                    self.assertIs(self.app.layout.current_control,
                                  self.composer.control)

    async def test_lone_escape_beats_composer_timeout(self):
        async with self.dialog_application() as (host, pipe):
            # Keep Application's normal terminal-sequence flush timeout here.
            self.app.ttimeoutlen = 0.5
            answer = self.request(host.confirm('Resume 2 stopped agents? [Y/n]',
                                               default=True, escape=False))
            await self.wait_modal(host)
            start = asyncio.get_running_loop().time()
            pipe.send_bytes(b'\x1b')
            self.assertFalse(await asyncio.wait_for(asyncio.shield(answer), 0.85))
            self.assertLess(asyncio.get_running_loop().time() - start, 0.85)

    async def test_cancelled_waiter_cleans_future_and_restores_focus(self):
        async with self.dialog_application() as (host, pipe):
            closed = host.body
            answer = self.request(host.confirm('Confirm?'))
            await self.wait_modal(host)
            future = host.future
            answer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await answer
            self.assertTrue(future.done())
            self.assertIsNone(host.future)
            self.assertIs(host.body, closed)
            self.assertIs(self.app.layout.current_control, self.composer.control)
            again = self.request(host.confirm('Again?'))
            await self.wait_modal(host)
            pipe.send_text('n')
            self.assertFalse(await self.result(again))

    def screen_text(self):
        screen = self.app.renderer.last_rendered_screen
        return '\n'.join(''.join(cell.char for _, cell in sorted(row.items()))
                         for _, row in sorted(screen.data_buffer.items()))

    async def test_form_typing_tab_backtab_and_radio_default(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.form('Agent', [
                Field('name', 'Name', required=True),
                Field('provider', 'Provider', default='local-z',
                      choices=('local-a', 'local-z'), required=True),
            ], submit_label='Start'))
            await self.wait_modal(host)
            pipe.send_text(' ynaq \t\x1b[Z!\t\r')
            result = await self.result(answer)
            self.assertEqual(result, ModalResult({'name': ' ynaq !',
                                                  'provider': 'local-z'}))
            self.assertEqual(self.composer.text, 'draft')

    async def test_required_error_retains_fields_until_explicit_submit(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.form('New agent', [Field('cwd', 'Directory',
                                                               required=True)],
                                            submit_label='Start', error='Earlier failure'))
            await self.wait_modal(host)
            self.assertIn('Earlier failure', self.screen_text())
            pipe.send_text('  \r')
            await asyncio.sleep(0.05)
            self.assertFalse(answer.done())
            self.assertIn('Directory is required', self.screen_text())
            pipe.send_text('/tmp\r')
            self.assertEqual(await self.result(answer), ModalResult({'cwd': '  /tmp'}))

    async def test_form_choice_navigation_and_first_configured_default(self):
        async with self.dialog_application() as (host, pipe):
            for keys, expected in [('\r', 'inert-first'), ('\x1b[B\r', 'inert-next')]:
                answer = self.request(host.form('Providers', [Field(
                    'provider', 'Provider', choices=('inert-first', 'inert-next'))],
                    submit_label='Use'))
                await self.wait_modal(host)
                pipe.send_text(keys)
                self.assertEqual(await self.result(answer), ModalResult({'provider': expected}))

    async def test_busy_requests_preserve_active_waiter_and_focus(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.confirm('Active?'))
            await self.wait_modal(host)
            future = host.future
            focus = self.app.layout.current_control
            self.assertTrue(await host.confirm('Busy?', escape=True))
            self.assertEqual(await host.form('Busy', [], submit_label='Go'),
                             ModalResult(cancelled=True))
            self.assertEqual(await host.choose('Busy', []), ModalResult(cancelled=True))
            self.assertIs(host.future, future)
            self.assertIs(self.app.layout.current_control, focus)
            self.assertFalse(future.done())
            self.assertFalse(answer.done())
            pipe.send_text('y')
            self.assertTrue(await self.result(answer))

    async def test_form_and_palette_escape_ctrl_c_cleanup(self):
        async with self.dialog_application() as (host, pipe):
            for factory in [lambda: host.form('Form', [Field('name', 'Name')],
                                              submit_label='Save'),
                            lambda: host.choose('Palette', [])]:
                for keys in ['\x1b', '\x03']:
                    answer = self.request(factory())
                    await self.wait_modal(host)
                    pipe.send_text(keys)
                    self.assertEqual(await self.result(answer), ModalResult(cancelled=True))
                    self.assertIsNone(host.future)
                    self.assertIs(self.app.layout.current_control, self.composer.control)

    async def test_palette_search_navigation_preserves_ids_and_order(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.choose('Actions', [
                {'id': 'z', 'label': 'Resume', 'description': 'Second session'},
                {'id': 'a', 'label': 'Resume', 'description': 'First session'},
                {'id': 'other', 'label': 'Archive'},
            ]))
            await self.wait_modal(host)
            pipe.send_text('rEsUmE\x1b[B')
            await asyncio.sleep(0.05)
            self.assertFalse(answer.done())
            pipe.send_text('\r')
            self.assertEqual(await self.result(answer), ModalResult('a'))

    async def test_disabled_palette_row_exposes_reason_without_activation(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.choose('Actions', [
                {'id': 'stop', 'label': 'Stop', 'disabled_reason': 'Requires tmux'},
                {'id': 'help', 'label': 'Help', 'description': 'Keyboard reference'},
            ], searchable=False))
            await self.wait_modal(host)
            self.assertIn('Requires tmux', self.screen_text())
            pipe.send_text('\r')
            await asyncio.sleep(0.05)
            self.assertFalse(answer.done())
            pipe.send_text('\x1b[B\r')
            self.assertEqual(await self.result(answer), ModalResult('help'))

    async def test_malicious_labels_are_plain_sanitized_text_raw_value_survives(self):
        bad = '\x1b[31m<x>\u202e\u200b\x07'
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.form(bad, [Field('name', bad, default=bad)],
                                            submit_label=bad, error=bad))
            await self.wait_modal(host)
            rendered = self.screen_text()
            for character in ['\x1b', '\u202e', '\u200b', '\x07']:
                self.assertNotIn(character, rendered)
            self.assertIn('<x>', rendered)
            pipe.send_text('\r')
            self.assertEqual(await self.result(answer), ModalResult({'name': bad}))
            answer = self.request(host.choose(bad, [{'id': bad, 'label': bad,
                                                    'description': bad}]))
            await self.wait_modal(host)
            for character in ['\x1b', '\u202e', '\u200b', '\x07']:
                self.assertNotIn(character, self.screen_text())
            pipe.send_text('\r')
            self.assertEqual(await self.result(answer), ModalResult(bad))

    async def test_confirmation_tab_enter_activates_visible_no_button(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.confirm('Continue?', default=True))
            await self.wait_modal(host)
            pipe.send_text('\t\r')
            self.assertFalse(await self.result(answer))

    async def test_form_tab_enter_cancel_keeps_draft(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.form('Name', [Field('name', 'Name')],
                                            submit_label='Save'))
            await self.wait_modal(host)
            pipe.send_text('draft\t\t\r')
            self.assertEqual(await self.result(answer), ModalResult(cancelled=True))
            self.assertEqual(self.composer.text, 'draft')

    async def test_palette_empty_search_stays_open_then_restores_selection_by_id(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.choose('Actions', [
                {'id': 'first', 'label': 'Resume'}, {'id': 'second', 'label': 'Rename'}]))
            await self.wait_modal(host)
            pipe.send_text('missing\r')
            await asyncio.sleep(0.05)
            self.assertFalse(answer.done())
            self.assertIn('No matching choices', self.screen_text())
            pipe.send_text('\x01\x0b\x1b[Bren')
            await asyncio.sleep(0.05)
            self.assertFalse(answer.done())
            pipe.send_text('\x01\x0b\r')
            self.assertEqual(await self.result(answer), ModalResult('second'))

    async def test_focus_restore_uses_surviving_control_and_finish_is_idempotent(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.confirm('Continue?'))
            await self.wait_modal(host)
            replacement = TextArea(text='replacement')
            self.app.layout.container.content = replacement.__pt_container__()
            host.cancel()
            host.finish(True)
            self.assertFalse(await self.result(answer))
            self.assertIs(self.app.layout.current_control, replacement.control)

    async def test_prior_waiter_cleanup_does_not_close_next_dialog(self):
        async with self.dialog_application() as (host, pipe):
            first = self.request(host.confirm('First?'))
            await self.wait_modal(host)
            host.finish(True)
            second = self.request(host.confirm('Second?'))
            await self.wait_modal(host)
            self.assertTrue(await self.result(first))
            self.assertFalse(second.done())
            pipe.send_text('n')
            self.assertFalse(await self.result(second))

    async def test_focus_restore_skips_control_hidden_during_modal(self):
        async with self.dialog_application() as (host, pipe):
            answer = self.request(host.confirm('Continue?'))
            await self.wait_modal(host)
            replacement = TextArea(text='replacement')
            self.app.layout.container.content = HSplit([
                ConditionalContainer(self.composer, filter=False), replacement])
            pipe.send_text('n')
            self.assertFalse(await self.result(answer))
            self.assertIs(self.app.layout.current_control, replacement.control)


if __name__ == '__main__':
    unittest.main()
