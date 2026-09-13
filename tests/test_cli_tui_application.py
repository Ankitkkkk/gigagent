"""Production application ownership with real input, controller and receiver."""
import asyncio
import copy
from contextvars import ContextVar
from contextlib import asynccontextmanager
import importlib.util
import json
import signal
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from cli import ChatClient
from cli_api import CLIError
from cli_workspaces import WorkspaceAPI
from cli_workspace_chat import WorkspaceChatController
from tests._tui_harness import application_harness
from tests.test_cli_tui_workflows import workspace, agent
from tests.test_cli_workspace_chat import ControlledSocket


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def ui(self, *, rows=(), selector=None, selected=None, plain=False,
                 no_resume=True, real_terminal=False, prior_hooks=None, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec('cli_tui'), 'TuiApplication missing')
        events = []
        records = {w['id']: copy.deepcopy(w) for w in rows}
        if selected:
            records[selected['id']] = copy.deepcopy(selected)
        api = Mock(spec=WorkspaceAPI)
        api.list.side_effect = lambda *, include_archived=False: {'workspaces': [copy.deepcopy(w)
            for w in records.values() if include_archived or not w['archived']]}
        api.get.side_effect = lambda ident: copy.deepcopy(records[ident])
        def action(ident, name, agent_id=None, body=None):
            events.append(name)
            if name in ('archive', 'unarchive'):
                records[ident]['archived'] = name == 'archive'
            if agent_id:
                return copy.deepcopy(next(a for a in records[ident]['agents'] if a['agent_id'] == agent_id))
            return copy.deepcopy(records[ident])
        api.action.side_effect = action
        client = ChatClient('http://127.0.0.1:18300', output=lambda text: None)
        client.channels = ['general', 'other']
        controller = WorkspaceChatController(client, api, selector=selector,
            plain_channel=plain, no_resume=no_resume, providers=['inert'])
        if selected:
            controller._select(copy.deepcopy(selected))
        if prior_hooks is not None:
            (client.output, client.on_view_change, client.on_workspace, client.on_settings,
             controller.presentation, controller.on_view_change) = prior_hooks
        socket = ControlledSocket()
        foreground_started, release_foreground = threading.Event(), threading.Event()
        release_foreground.set()
        def runner(argv, **options):
            if argv[1] == 'has-session':
                return subprocess.CompletedProcess(argv, 0)
            events.append('foreground_started')
            foreground_started.set()
            if not release_foreground.wait(5):
                raise AssertionError('foreground was not released')
            events.append('foreground_finished')
            return subprocess.CompletedProcess(argv, 0)
        @asynccontextmanager
        async def terminal():
            events.append('terminal_enter')
            try:
                yield
            finally:
                events.append('terminal_restored')
        original_receive = client.receive_forever
        async def receive():
            events.append('receiver_start')
            try:
                await original_receive()
            finally:
                events.append('receiver_cancel')
        client.receive_forever = receive
        original_poll = controller.poll_forever
        async def poll():
            events.append('poller_start')
            try:
                await original_poll()
            finally:
                events.append('poller_cancel')
        controller.poll_forever = poll
        if not real_terminal:
            kwargs['terminal_context'] = terminal
        with patch('cli.fetch_session_token', return_value='inert-token'), \
             patch('websockets.asyncio.client.connect', return_value=socket), \
             patch.dict('os.environ', {'TMUX': ''}):
            async with application_harness(client, controller, runner=runner, **kwargs) as ui:
                ui.events, ui.records, ui.socket = events, records, socket
                ui.release_foreground, ui.foreground_started = release_foreground, foreground_started
                try:
                    yield ui
                finally:
                    release_foreground.set()

    async def modal(self, ui, text):
        await ui.wait_until(lambda: ui.dialogs.future is not None and text in ui.screen_text())

    async def connected(self, ui):
        await ui.wait_until(lambda: ui.client.websocket is not None)
        await ui.socket.deliver({'type': 'history_complete'})

    async def test_initial_cancel_never_starts_transport(self):
        for key in ('Escape', 'CtrlC', 'CtrlQ'):
            with self.subTest(key=key):
                async with self.ui(rows=[workspace()]) as ui:
                    await self.modal(ui, 'Show archived')
                    self.assertNotIn('receiver_start', ui.events)
                    await ui.key(key)
                    await asyncio.wait_for(ui.task, 2)
                    self.assertEqual(ui.events, [])
                    self.assertIsNone(ui.dialogs.future)

    async def test_explicit_selector_decline_propagates_after_cleanup(self):
        async with self.ui(rows=[workspace(archived=True)], selector='One') as ui:
            await self.modal(ui, 'Unarchive it? [y/N]')
            await ui._send('n')
            with self.assertRaisesRegex(CLIError, '^Archived session was not selected$'):
                await asyncio.wait_for(ui.task, 2)
            self.assertEqual(ui.events, [])
            self.assertIsNone(ui.controller.presentation)

    async def test_selected_tasks_survive_navigation_then_checkpoint_before_stop(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            await ui.key('F2')
            await self.modal(ui, 'Show archived')
            await ui.key('Escape')
            self.assertEqual(ui.events.count('receiver_start'), 1)
            self.assertEqual(ui.events.count('poller_start'), 1)
            await ui.tui.request_quit(signal=True)
            self.assertLess(ui.events.index('checkpoint'), ui.events.index('receiver_cancel'))
            self.assertLess(ui.events.index('checkpoint'), ui.events.index('poller_cancel'))

    async def test_attach_signal_waits_for_foreground_before_restore(self):
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            ui.release_foreground.clear()
            attach = asyncio.create_task(ui.tui.attach(ui.controller.workspace['agents'][0]))
            await asyncio.wait_for(asyncio.to_thread(ui.foreground_started.wait, 2), 3)
            quit_task = asyncio.create_task(ui.tui.request_quit(signal=True))
            await ui.socket.deliver({'type': 'message', 'data': {
                'id': 91, 'channel': 'ws_one', 'text': 'still receiving', 'sender': 'human'}})
            self.assertEqual(ui.client.messages[91]['text'], 'still receiving')
            self.assertNotIn('terminal_restored', ui.events)
            self.assertFalse(quit_task.done())
            ui.release_foreground.set()
            await asyncio.wait_for(asyncio.gather(attach, quit_task), 3)
            self.assertLess(ui.events.index('foreground_finished'), ui.events.index('terminal_restored'))
            self.assertLess(ui.events.index('checkpoint'), ui.events.index('receiver_cancel'))

    async def test_modal_quit_decline_keeps_draft_and_help_closes(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            await ui.type_text('unsent')
            await ui.key('F2')
            await self.modal(ui, 'Show archived')
            await ui.key('F1')
            await ui.key('CtrlQ')
            await self.modal(ui, 'unsent')
            self.assertFalse(ui.view.help_visible)
            await ui._send('n')
            self.assertEqual(ui.view.composer.text, 'unsent')
            self.assertFalse(ui.task.done())

    async def test_slash_quit_inside_composer_does_not_self_wait(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            await ui.type_text('/quit')
            await ui.key('Enter')
            await asyncio.wait_for(ui.task, 2)
            self.assertEqual(ui.events.count('checkpoint'), 1)

    async def test_real_in_terminal_preserves_context_and_waits_before_redraw(self):
        from prompt_toolkit.application import Application
        from prompt_toolkit.application.current import create_app_session, get_app_or_none, set_app
        async with self.ui(selected=workspace(agents=[agent()]), real_terminal=True) as ui:
            await self.connected(ui)
            ui.release_foreground.clear()
            restored = []
            async def external_attach():
                with create_app_session(input=ui.pipe, output=ui.application.output):
                    sentinel = Application(input=ui.pipe, output=ui.application.output)
                    with set_app(sentinel):
                        result = await ui.tui.attach(ui.controller.workspace['agents'][0])
                        restored.append(get_app_or_none() is sentinel)
                        return result
            action = asyncio.create_task(external_attach())
            self.assertTrue(await asyncio.to_thread(ui.foreground_started.wait, 2))
            self.assertTrue(ui.application._running_in_terminal)
            count = ui.render_count
            quit_task = asyncio.create_task(ui.tui.request_quit(signal=True))
            await ui.socket.deliver({'type': 'message', 'data': {
                'id': 42, 'text': 'real handoff receiving', 'channel': 'ws_one'}})
            self.assertIn(42, ui.client.messages)
            self.assertEqual(ui.render_count, count)
            self.assertFalse(quit_task.done())
            ui.release_foreground.set()
            await asyncio.wait_for(asyncio.gather(action, quit_task), 3)
            self.assertFalse(ui.application._running_in_terminal)
            self.assertEqual(restored, [True])
            self.assertLess(ui.events.index('foreground_finished'), ui.events.index('checkpoint'))

    async def test_attach_caller_repeated_cancellation_does_not_release_terminal(self):
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            ui.release_foreground.clear()
            action = asyncio.create_task(ui.tui.attach(ui.controller.workspace['agents'][0]))
            self.assertTrue(await asyncio.to_thread(ui.foreground_started.wait, 2))
            action.cancel()
            await ui.socket.deliver({'type': 'status', 'data': {}})
            action.cancel()
            await ui.socket.deliver({'type': 'status', 'data': {}})
            self.assertFalse(action.done())
            self.assertNotIn('terminal_restored', ui.events)
            ui.release_foreground.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(action, 2)
            self.assertIn('terminal_restored', ui.events)

    async def test_preflight_registration_prevents_quit_race_and_refusal_suspension(self):
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            entered, release = threading.Event(), threading.Event()
            def probe(argv, **kwargs):
                self.assertEqual(argv[1], 'has-session')
                entered.set()
                if not release.wait(3):
                    raise AssertionError('probe not released')
                return subprocess.CompletedProcess(argv, 0)
            ui.tui.runner = probe
            attach = asyncio.create_task(ui.tui.attach(ui.controller.workspace['agents'][0]))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            quitting = asyncio.create_task(ui.tui.request_quit(signal=True))
            try:
                await ui.socket.deliver({'type': 'status', 'data': {}})
                self.assertFalse(quitting.done())
                self.assertNotIn('terminal_enter', ui.events)
            finally:
                release.set()
            outcome, _ = await asyncio.wait_for(asyncio.gather(attach, quitting), 3)
            self.assertEqual(outcome.status, 'cancelled')
            self.assertNotIn('terminal_enter', ui.events)

    async def test_attach_missing_target_and_nonzero_recovery_are_sanitized(self):
        for codes, wanted in [([1], 'not running; resume with /resume ag_one'),
                              ([0, 1, 1], 'not running; resume with /resume ag_one'),
                              ([0, 1, 0], 'Agent terminal attach failed. Check tmux and retry.')]:
            with self.subTest(codes=codes):
                async with self.ui(selected=workspace(agents=[agent()])) as ui:
                    await self.connected(ui)
                    queue = iter(codes)
                    ui.tui.runner = lambda argv, **kwargs: subprocess.CompletedProcess(argv, next(queue))
                    outcome = await ui.tui.attach(ui.controller.workspace['agents'][0])
                    self.assertEqual(outcome.status, 'failed')
                    self.assertEqual(ui.state.notices.lines[-1], wanted)
                    self.assertEqual('terminal_enter' in ui.events, len(codes) > 1)
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            def failure(*args, **kwargs):
                raise OSError('secret credential')
            ui.tui.runner = failure
            outcome = await ui.tui.attach(ui.controller.workspace['agents'][0])
            self.assertNotIn('secret', outcome.message)
            self.assertNotIn('terminal_enter', ui.events)

    async def test_nested_attach_uses_switch_client_and_notice(self):
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0)
            ui.tui.runner = runner
            with patch.dict('os.environ', {'TMUX': 'inert-nested'}):
                await ui.tui.attach(ui.controller.workspace['agents'][0])
            await ui.wait_render()
            self.assertEqual(calls, [['tmux', 'has-session', '-t', '=inert'],
                                     ['tmux', 'switch-client', '-t', 'inert']])
            self.assertIn('Switch back: tmux switch-client -l', ui.state.notices.lines)

    async def test_required_navigation_owning_modal_survives_quit_decline(self):
        async with self.ui(selected=workspace(), rows=[workspace('ws_two', 'Two')]) as ui:
            await self.connected(ui)
            await ui.type_text('kept draft')
            await ui.controller.execute_action('archive_session', {'confirmed': True})
            navigation = asyncio.create_task(ui.tui.navigate(mandatory=True))
            try:
                await self.modal(ui, 'Show archived')
                await ui.type_text('two')
                await ui.key('CtrlQ')
                await self.modal(ui, 'Quit with unsent')
                self.assertFalse(ui.dialogs.owner_is_short_lived(ui.dialogs.owner))
                await ui._send('n')
                await self.modal(ui, 'Show archived')
                self.assertFalse(navigation.done())
                self.assertEqual(ui.state.search, 'two')
                self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'kept draft')
                await ui.key('Escape')
                await self.modal(ui, 'Quit with unsent')
                await ui._send('y')
                await asyncio.wait_for(ui.task, 2)
                self.assertNotIn('checkpoint', ui.events)
            finally:
                navigation.cancel()
                await asyncio.gather(navigation, return_exceptions=True)

    async def test_required_navigation_waiting_behind_modal_decline_then_signal(self):
        async with self.ui(selected=workspace(), rows=[workspace('ws_two', 'Two')]) as ui:
            await self.connected(ui)
            await ui.type_text('kept draft')
            await ui.controller.execute_action('archive_session', {'confirmed': True})
            other = asyncio.create_task(ui.tui.confirm('Other modal?', escape=False))
            await self.modal(ui, 'Other modal?')
            navigation = asyncio.create_task(ui.tui.navigate(mandatory=True))
            try:
                await ui.wait_render()
                self.assertFalse(navigation.done())
                await ui.key('CtrlQ')
                await self.modal(ui, 'Quit with unsent')
                await ui._send('n')
                await self.modal(ui, 'Show archived')
                self.assertFalse(navigation.done())
                await ui.tui.request_quit(signal=True)
                calls = ui.api.list.call_count
                await asyncio.wait_for(ui.task, 2)
                self.assertEqual(ui.api.list.call_count, calls)
                self.assertIsNone(ui.dialogs.future)
                self.assertNotIn('checkpoint', ui.events)
                self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'kept draft')
            finally:
                other.cancel()
                navigation.cancel()
                await asyncio.gather(other, navigation, return_exceptions=True)

    async def test_archive_refresh_cannot_open_picker_over_competing_commit(self):
        async with self.ui(selected=workspace(), rows=[workspace('ws_two', 'Two')]) as ui:
            await self.connected(ui)
            entered, release = asyncio.Event(), asyncio.Event()
            original = ui.workflows.refresh_sessions
            first = True
            async def refresh():
                nonlocal first
                if first:
                    first = False
                    entered.set()
                    await release.wait()
                return await original()
            ui.workflows.refresh_sessions = refresh
            archive = asyncio.create_task(ui.tui.run_action('archive_session'))
            try:
                await self.modal(ui, 'Archive session?')
                await ui._send('y')
                await asyncio.wait_for(entered.wait(), 2)
                await ui.key('F2')
                await self.modal(ui, 'Show archived')
                await ui.select_row('ws_two')
                await ui.wait_until(lambda: ui.controller.workspace is not None)
                release.set()
                outcome = await asyncio.wait_for(archive, 2)
                self.assertEqual(outcome.status, 'completed')
                self.assertIsNone(ui.dialogs.future)
                self.assertEqual(ui.controller.workspace['id'], 'ws_two')
                self.assertEqual(ui.events.count('receiver_start'), 2)
                self.assertEqual(ui.events.count('poller_start'), 2)
            finally:
                release.set()
                archive.cancel()
                await asyncio.gather(archive, return_exceptions=True)

    async def test_plain_channel_transition_and_settings_before_rename_preserve_drafts(self):
        async with self.ui(plain=True) as ui:
            await self.connected(ui)
            await ui.type_text('general draft')
            await ui.tui.submit('/join other')
            self.assertEqual(ui.composer_actions.key, ('channel', 'other'))
            await ui.type_text('old draft')
            await ui.socket.deliver({'type': 'settings', 'data': {'channels': ['general', 'new']}})
            self.assertEqual(ui.composer_actions.key, ('channel', 'general'))
            await ui.socket.deliver({'type': 'channel_renamed', 'old_name': 'other', 'new_name': 'new'})
            self.assertEqual(ui.state.drafts.get(('channel', 'new')), 'old draft')
            self.assertEqual(ui.state.drafts.get(('channel', 'general')), 'general draft')
            await ui.tui.submit('/create new')
            self.assertEqual(ui.view.composer.text, 'old draft')
            self.assertNotIn('poller_start', ui.events)

    async def test_initial_notices_and_thread_output_use_activity_then_restore_hooks(self):
        async with self.ui(plain=True, initial_notices=('startup warning',)) as ui:
            await self.connected(ui)
            await asyncio.to_thread(ui.client.show, 'worker output')
            await ui.wait_render()
            self.assertIn('startup warning', ui.state.notices.lines)
            self.assertIn('worker output', ui.state.notices.lines)
            await ui.tui.request_quit(signal=True)
            await ui.task
            self.assertIsNone(ui.client.on_view_change)
            self.assertIsNone(ui.client.on_settings)
            self.assertIsNone(ui.controller.presentation)

    async def test_missing_and_ambiguous_selector_raise_without_starting_transport(self):
        for selector, rows in [('missing', [workspace()]),
                               ('Same', [workspace('ws_one', 'Same'), workspace('ws_two', 'Same')])]:
            with self.subTest(selector=selector):
                async with self.ui(rows=rows, selector=selector) as ui:
                    with self.assertRaises(CLIError):
                        await asyncio.wait_for(ui.task, 2)
                    self.assertEqual(ui.events, [])
                    self.assertIsNone(ui.controller.presentation)

    async def test_quit_cancels_precommit_read_without_selecting_after_read_finishes(self):
        async with self.ui(selected=workspace(), rows=[workspace('ws_two', 'Two')]) as ui:
            await self.connected(ui)
            entered, release = threading.Event(), threading.Event()
            def get(ident):
                entered.set()
                release.wait(3)
                return copy.deepcopy(ui.records[ident])
            ui.api.get.side_effect = get
            selection = asyncio.create_task(ui.tui.run_action('select_session', target_id='ws_two'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await asyncio.wait_for(ui.tui.request_quit(signal=True), 2)
                self.assertEqual(ui.controller.workspace['id'], 'ws_one')
                self.assertEqual(ui.events.count('checkpoint'), 1)
            finally:
                release.set()
                await asyncio.gather(selection, return_exceptions=True)
            self.assertEqual(ui.controller.workspace['id'], 'ws_one')

    async def test_quit_drains_selection_commit_then_checkpoints_new_session(self):
        async with self.ui(selected=workspace(), rows=[workspace('ws_two', 'Two')]) as ui:
            await self.connected(ui)
            entered, release = threading.Event(), threading.Event()
            checkpoints = []
            def action(ident, name, **kwargs):
                if name == 'checkpoint':
                    checkpoints.append(ident)
                    if ident == 'ws_one':
                        entered.set()
                        release.wait(3)
                    ui.events.append('checkpoint')
                return copy.deepcopy(ui.records[ident])
            ui.api.action.side_effect = action
            selection = asyncio.create_task(ui.tui.run_action('select_session', target_id='ws_two'))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            quitting = asyncio.create_task(ui.tui.request_quit(signal=True))
            try:
                await ui.socket.deliver({'type': 'status', 'data': {}})
                self.assertFalse(quitting.done())
                self.assertNotIn('receiver_cancel', ui.events)
            finally:
                release.set()
            await asyncio.wait_for(asyncio.gather(selection, quitting, return_exceptions=True), 3)
            self.assertEqual(checkpoints, ['ws_one', 'ws_two'])
            self.assertEqual(ui.controller.workspace['id'], 'ws_two')
            self.assertEqual(ui.events[-2:], ['receiver_cancel', 'poller_cancel'])

    async def test_quit_cancels_queued_action_before_pending_mutation_drain(self):
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            entered, release = threading.Event(), threading.Event()
            def rename(ident, name):
                entered.set()
                release.wait(3)
                ui.records[ident]['name'] = name
                return copy.deepcopy(ui.records[ident])
            ui.api.rename.side_effect = rename
            mutation = asyncio.create_task(ui.tui.submit('/rename Changed'))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            queued = asyncio.create_task(ui.tui.submit('/stop ag_one'))
            await ui.socket.deliver({'type': 'status', 'data': {}})
            quitting = asyncio.create_task(ui.tui.request_quit(signal=True))
            try:
                await ui.socket.deliver({'type': 'status', 'data': {}})
                self.assertFalse(quitting.done())
                self.assertNotIn('stop', ui.events)
            finally:
                release.set()
            await asyncio.wait_for(asyncio.gather(mutation, queued, quitting, return_exceptions=True), 3)
            self.assertNotIn('stop', ui.events)
            self.assertEqual(ui.controller.workspace['name'], 'Changed')
            self.assertEqual(ui.api.rename.call_count, 1)

    async def test_registered_sigterm_upgrades_draft_confirmation_and_stops_navigation(self):
        async with self.ui(selected=workspace(), rows=[workspace('ws_two', 'Two')]) as ui:
            await self.connected(ui)
            await ui.type_text('draft')
            await ui.controller.execute_action('archive_session', {'confirmed': True})
            navigation = asyncio.create_task(ui.tui.navigate(mandatory=True))
            try:
                await self.modal(ui, 'Show archived')
                await ui.key('CtrlQ')
                await self.modal(ui, 'Quit with unsent')
                self.assertFalse(ui.dialogs.owner_is_short_lived(ui.dialogs.owner))
                asyncio.get_running_loop()._signal_handlers[signal.SIGTERM]._run()
                await asyncio.wait_for(ui.task, 2)
                self.assertIsNone(ui.dialogs.future)
                self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'draft')
                self.assertNotIn('checkpoint', ui.events)
            finally:
                await asyncio.gather(navigation, return_exceptions=True)

    async def test_prior_asyncio_sigterm_callback_and_context_are_restored(self):
        loop = asyncio.get_running_loop()
        old = signal.getsignal(signal.SIGTERM)
        value, observed = ContextVar('prior-signal-context', default='default'), []
        token = value.set('saved')
        loop.add_signal_handler(signal.SIGTERM, lambda: observed.append(value.get()))
        value.reset(token)
        try:
            loop._signal_handlers[signal.SIGTERM]._run()
            self.assertEqual(observed, ['saved'])
            async with self.ui(plain=True) as ui:
                await self.connected(ui)
                loop._signal_handlers[signal.SIGTERM]._run()
                await asyncio.wait_for(ui.task, 2)
            loop._signal_handlers[signal.SIGTERM]._run()
            self.assertEqual(observed, ['saved', 'saved'])
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, old)

    async def test_signal_fallback_and_no_prior_registration_restore_process_handlers(self):
        loop = asyncio.get_running_loop()
        old_int, old_term = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        async with self.ui(plain=True) as ui:
            await self.connected(ui)
            # Synthetic SIGINT remains separate from composer Ctrl+C.
            await ui.type_text('preserved')
            await ui.key('CtrlC')
            self.assertFalse(ui.task.done())
            ui.application.key_processor.send_sigint()
            await asyncio.wait_for(ui.task, 2)
        self.assertEqual(signal.getsignal(signal.SIGINT), old_int)
        self.assertEqual(signal.getsignal(signal.SIGTERM), old_term)
        self.assertNotIn(signal.SIGTERM, loop._signal_handlers)
        original_add = loop.add_signal_handler
        def unsupported(signum, *args):
            if signum in (signal.SIGINT, signal.SIGTERM):
                raise NotImplementedError
            return original_add(signum, *args)
        with patch.object(loop, 'add_signal_handler', side_effect=unsupported):
            async with self.ui(plain=True) as ui:
                await self.connected(ui)
                installed = signal.getsignal(signal.SIGTERM)
                self.assertTrue(callable(installed))
                installed(signal.SIGTERM, None)
                await asyncio.wait_for(ui.task, 2)
        self.assertEqual(signal.getsignal(signal.SIGINT), old_int)
        self.assertEqual(signal.getsignal(signal.SIGTERM), old_term)

    async def test_run_cancellation_waits_for_foreground_and_restores_hooks(self):
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            ui.release_foreground.clear()
            action = asyncio.create_task(ui.tui.attach(ui.controller.workspace['agents'][0]))
            self.assertTrue(await asyncio.to_thread(ui.foreground_started.wait, 2))
            ui.task.cancel()
            await ui.socket.deliver({'type': 'status', 'data': {}})
            self.assertFalse(ui.task.done())
            self.assertNotIn('terminal_restored', ui.events)
            ui.release_foreground.set()
            await asyncio.wait_for(action, 3)
            with self.assertRaises(asyncio.CancelledError):
                await ui.task
            self.assertIsNone(ui.client.on_view_change)
            self.assertIn('terminal_restored', ui.events)

    async def test_plain_pending_channel_and_rename_collision_reconcile_without_loss(self):
        async with self.ui(plain=True) as ui:
            await self.connected(ui)
            async def send(raw):
                ui.events.append(raw)
            ui.socket.send = send
            await ui.type_text('original')
            await ui.tui.submit('/create created')
            self.assertEqual(ui.composer_actions.key, ('channel', 'general'))
            await ui.socket.deliver({'type': 'settings', 'data': {'channels': ['general', 'created']}})
            self.assertEqual(ui.composer_actions.key, ('channel', 'created'))
            await ui.type_text('destination')
            await ui.socket.deliver({'type': 'channel_renamed', 'old_name': 'general', 'new_name': 'created'})
            self.assertEqual(ui.view.composer.text, 'destination')
            self.assertEqual(ui.state.drafts.get(('channel', 'general')), 'original')
            self.assertEqual(ui.state.notices.lines.count('Channel draft collision; both drafts were kept'), 1)

    async def test_selection_during_receiver_cancellation_reconciles_latest_destination(self):
        stopping, release = asyncio.Event(), asyncio.Event()
        token_started, release_token = threading.Event(), threading.Event()
        async def slow_exit(socket, *args):
            stopping.set()
            await release.wait()
            return False
        def token(url):
            token_started.set()
            if not release_token.wait(3):
                raise AssertionError('replacement token fetch not released')
            return 'inert-token'
        with patch.object(ControlledSocket, '__aexit__', slow_exit):
            async with self.ui(selected=workspace(), rows=[workspace('ws_two', 'Two')]) as ui:
                await self.connected(ui)
                with patch('cli.fetch_session_token', side_effect=token):
                    try:
                        await ui.controller.execute_action('archive_session', {'confirmed': True})
                        await asyncio.wait_for(stopping.wait(), 2)
                        selection = await ui.tui.run_action('select_session', target_id='ws_two')
                        self.assertEqual(selection.status, 'completed')
                        release.set()
                        self.assertTrue(await asyncio.to_thread(token_started.wait, 2))
                        self.assertEqual(ui.events.count('receiver_start'), 2)
                        self.assertIsNone(ui.client.websocket)
                        self.assertFalse(ui.tui.receiver_task.done())
                        release_token.set()
                        await self.connected(ui)
                        await ui.socket.deliver({'type': 'message', 'data': {
                            'id': 999, 'channel': 'ws_two', 'text': 'replacement delivered'}})
                        self.assertEqual(ui.client.messages[999]['text'], 'replacement delivered')
                        self.assertEqual(ui.client.messages[999]['channel'], 'ws_two')
                        self.assertEqual(ui.events.count('receiver_start'), 2)
                        self.assertFalse(ui.tui.poller_task.done())
                        self.assertEqual(ui.controller.workspace['id'], 'ws_two')
                    finally:
                        release.set()
                        release_token.set()

    async def test_unexpected_workflow_error_exits_cleanly_and_reaches_run_caller(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            with patch.object(ui.workflows, 'run_action', side_effect=RuntimeError('local workflow bug')):
                outcome = await ui.tui.run_action('help')
            self.assertEqual(outcome.status, 'failed')
            with self.assertRaisesRegex(RuntimeError, 'local workflow bug'):
                await asyncio.wait_for(ui.task, 2)
            self.assertIsNone(ui.controller.presentation)
            self.assertIsNone(ui.client.on_view_change)
            self.assertIsNone(ui.dialogs.future)

    async def test_submit_presentation_commands_delegate_and_failures_keep_draft(self):
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            await ui.type_text('/join other')
            await ui.key('Enter')
            self.assertEqual(ui.view.composer.text, '/join other')
            self.assertEqual(ui.client.channel, 'ws_one')
            await ui.tui.submit('/help')
            self.assertTrue(ui.view.help_visible)
            ui.view.hide_help()
            for command in ('/sessions extra', '/archive extra'):
                outcome = await asyncio.wait_for(ui.tui.submit(command), 1)
                self.assertEqual(outcome.status, 'failed')
                self.assertIsNone(ui.dialogs.future)
            ui.api.rename.side_effect = lambda ident, name: dict(ui.records[ident], name=name)
            result = await ui.tui.submit('/rename Renamed')
            self.assertEqual(result.status, 'completed')
            self.assertEqual(ui.controller.workspace['name'], 'Renamed')

    async def test_plain_history_arguments_and_chat_use_existing_transport(self):
        async with self.ui(plain=True) as ui:
            await self.connected(ui)
            frames = []
            async def send(raw):
                frames.append(json.loads(raw))
            ui.socket.send = send
            await ui.socket.deliver({'type': 'message', 'data': {
                'id': 9, 'text': 'history message', 'channel': 'general'}})
            result = await ui.tui.submit('/history anything')
            self.assertEqual(result.status, 'completed')
            self.assertEqual(frames, [])
            result = await ui.tui.submit('/continue')
            self.assertTrue(result.sent)
            self.assertEqual(frames[0]['text'], '/continue')
            self.assertEqual(frames[0]['channel'], 'general')

    async def test_nested_archive_navigation_callback_selects_once(self):
        async with self.ui(selected=workspace(), rows=[workspace('ws_two', 'Two')]) as ui:
            await self.connected(ui)
            await ui.key('F2')
            await self.modal(ui, 'Show archived')
            await ui.activate_named('more_actions')
            await self.modal(ui, 'Session actions: One')
            await ui.type_text('Archive')
            await ui.key('Enter')
            await self.modal(ui, 'Archive session?')
            await ui._send('y')
            await self.modal(ui, 'Show archived')
            self.assertIsNone(ui.controller.workspace)
            self.assertIsNone(ui.tui.receiver_task)
            self.assertIsNone(ui.tui.poller_task)
            await ui.select_row('ws_two')
            await ui.wait_until(lambda: ui.client.websocket is not None)
            self.assertIsNone(ui.dialogs.future)
            self.assertEqual(ui.controller.workspace['id'], 'ws_two')
            self.assertEqual(ui.events.count('receiver_start'), 2)
            self.assertEqual(ui.events.count('poller_start'), 2)

    async def test_forced_channel_fallback_at_capacity_keeps_all_drafts(self):
        async with self.ui(plain=True) as ui:
            await self.connected(ui)
            await ui.tui.submit('/join other')
            for index in range(50):
                self.assertTrue(ui.state.drafts.set(('channel', 'saved-' + str(index)), 'saved'))
            await ui.socket.deliver({'type': 'settings', 'data': {'channels': ['general']}})
            self.assertEqual(ui.composer_actions.key, ('channel', 'general'))
            await ui.type_text('refused')
            self.assertEqual(ui.view.composer.text, '')
            self.assertEqual(len(ui.state.drafts), 50)
            self.assertEqual(ui.state.notices.lines.count('50 unsent drafts; send or clear one'), 1)

    async def test_foreground_exception_restores_terminal_without_diagnostics(self):
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            def runner(argv, **kwargs):
                if argv[1] == 'has-session':
                    return subprocess.CompletedProcess(argv, 0)
                raise subprocess.SubprocessError('credential in diagnostics')
            ui.tui.runner = runner
            outcome = await ui.tui.attach(ui.controller.workspace['agents'][0])
            self.assertEqual(outcome.status, 'failed')
            self.assertIn('terminal_restored', ui.events)
            self.assertNotIn('credential', '\n'.join(ui.state.notices.lines))

    async def test_reconnect_refreshes_metadata_without_restarting_receiver_task(self):
        async def frames(socket):
            while True:
                frame = await socket.frames.get()
                if frame is None:
                    return
                yield json.dumps(frame)
                socket.processed.put_nowait(None)
        with patch.object(ControlledSocket, '__aiter__', frames):
            async with self.ui(selected=workspace()) as ui:
                await self.connected(ui)
                ui.records['ws_one']['name'] = 'Remote rename'
                await ui.socket.frames.put(None)
                await ui.wait_until(lambda: ui.client.websocket is None)
                await ui.wait_until(lambda: any(row['name'] == 'Remote rename'
                    for row in ui.view.session_rows()), timeout=5)
                self.assertEqual(ui.events.count('receiver_start'), 1)
                self.assertEqual(ui.events.count('poller_start'), 1)

    async def test_key_quit_cancels_candidate_before_draft_confirmation_decline(self):
        async with self.ui(selected=workspace(),
                           rows=[workspace('ws_two', 'Two', agents=[agent()])], no_resume=False) as ui:
            await self.connected(ui)
            await ui.type_text('saved draft')
            selection = asyncio.create_task(ui.tui.run_action('select_session', target_id='ws_two'))
            await self.modal(ui, 'Resume 1 stopped agents?')
            await ui.key('CtrlQ')
            await self.modal(ui, 'Quit with unsent')
            self.assertEqual(ui.controller.workspace['id'], 'ws_one')
            await ui._send('n')
            await asyncio.wait_for(asyncio.gather(selection, return_exceptions=True), 2)
            self.assertEqual(ui.controller.workspace['id'], 'ws_one')
            self.assertEqual(ui.view.composer.text, 'saved draft')
            self.assertNotIn('checkpoint', ui.events)
            self.assertNotIn('resume', ui.events)

    async def test_explicit_selector_populates_sidebar_from_initial_list(self):
        async with self.ui(rows=[workspace(), workspace('ws_two', 'Two')], selector='One') as ui:
            await self.connected(ui)
            await ui.wait_render()
            self.assertEqual({row['id'] for row in ui.view.session_rows()}, {'ws_one', 'ws_two'})
            self.assertIn('Two', ui.screen_text())
            self.assertEqual(ui.api.list.call_count, 1)

    async def test_archive_completing_during_quit_decision_resumes_required_navigation(self):
        for answer in ('n', 'y'):
            with self.subTest(answer=answer):
                async with self.ui(selected=workspace(), rows=[workspace('ws_two', 'Two')]) as ui:
                    await self.connected(ui)
                    await ui.type_text('unsent before archive')
                    entered, release = threading.Event(), threading.Event()
                    original = ui.api.action.side_effect
                    def action(ident, name, **kwargs):
                        if name == 'archive':
                            entered.set()
                            if not release.wait(3):
                                raise AssertionError('archive not released')
                        return original(ident, name, **kwargs)
                    ui.api.action.side_effect = action
                    archive = asyncio.create_task(ui.tui.run_action('archive_session'))
                    try:
                        await self.modal(ui, 'Archive session?')
                        await ui._send('y')
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        await ui.key('CtrlQ')
                        await self.modal(ui, 'Quit with unsent')
                        release.set()
                        await ui.wait_until(lambda: ui.controller.workspace is None)
                        await ui._send(answer)
                        if answer == 'n':
                            await self.modal(ui, 'Show archived')
                            self.assertFalse(archive.done())
                            self.assertEqual(ui.state.drafts.get(('session', 'ws_one')), 'unsent before archive')
                        else:
                            await asyncio.wait_for(ui.task, 2)
                            self.assertIsNone(ui.dialogs.future)
                        self.assertNotIn('checkpoint', ui.events)
                    finally:
                        release.set()
                        archive.cancel()
                        await asyncio.gather(archive, return_exceptions=True)

    async def test_receiver_view_failure_reaches_run_after_restoration(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            failure = RuntimeError('local view bug')
            refresh = ui.view.refresh
            def fail_message(event=None):
                if event is not None and event.kind == 'messages':
                    raise failure
                return refresh(event)
            ui.view.refresh = fail_message
            await ui.socket.frames.put({'type': 'message', 'data': {
                'id': 333, 'channel': 'ws_one', 'text': 'trigger view'}})
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(asyncio.shield(ui.task), 1)
            self.assertIs(caught.exception, failure)
            self.assertIsNone(ui.client.on_view_change)
            self.assertIsNone(ui.controller.presentation)
            self.assertTrue(ui.tui.poller_task is None or ui.tui.poller_task.done())

    async def test_poller_view_failure_reaches_run_after_restoration(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            failure = RuntimeError('local poll view bug')
            refresh = ui.view.refresh
            def fail_workspace(event=None):
                if event is not None and event.source == 'controller' and event.kind == 'agent_state':
                    raise failure
                return refresh(event)
            ui.view.refresh = fail_workspace
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(asyncio.shield(ui.task), 3)
            self.assertIs(caught.exception, failure)
            self.assertIsNone(ui.client.on_workspace)
            self.assertIsNone(ui.controller.presentation)
            self.assertTrue(ui.tui.receiver_task is None or ui.tui.receiver_task.done())

    async def test_quit_during_open_form_returns_cancelled_without_mutation(self):
        hooks = (Mock(), Mock(), Mock(), Mock(), object(), Mock())
        async with self.ui(selected=workspace(), prior_hooks=hooks) as ui:
            await self.connected(ui)
            await ui.type_text('saved draft')
            rename = asyncio.create_task(ui.tui.run_action('rename_session'))
            try:
                await self.modal(ui, 'Rename session')
                await ui.type_text('edited form')
                await ui.key('CtrlQ')
                await self.modal(ui, 'Quit with unsent')
                await ui._send('n')
                self.assertEqual((await asyncio.wait_for(rename, 1)).status, 'cancelled')
                self.assertEqual(ui.api.rename.call_count, 0)
                self.assertEqual(ui.controller.workspace['name'], 'One')
                self.assertEqual(ui.view.composer.text, 'saved draft')
                await ui.tui.request_quit(signal=True)
                await ui.task
                actual = (ui.client.output, ui.client.on_view_change, ui.client.on_workspace,
                          ui.client.on_settings, ui.controller.presentation, ui.controller.on_view_change)
                for restored, original in zip(actual, hooks):
                    self.assertIs(restored, original)
            finally:
                rename.cancel()
                await asyncio.gather(rename, return_exceptions=True)

    async def test_spawned_refresh_failure_is_retrieved_and_reaches_run(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            failure = RuntimeError('refresh observer bug')
            with patch.object(ui.workflows, 'refresh_sessions', side_effect=failure):
                ui.client._set_connection_state('connected', force=True)
                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.wait_for(asyncio.shield(ui.task), 1)
                self.assertIs(caught.exception, failure)

    async def test_failing_signal_role_records_error_without_rescheduling(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            failure = RuntimeError('signal task failed')
            async def fail():
                raise failure
            with patch.object(ui.tui, '_schedule_signal') as schedule:
                task = ui.tui._spawn(fail(), 'signal')
                result = await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True), 1)
                self.assertIs(result[0], failure)
                await asyncio.sleep(0)
                schedule.assert_not_called()
            self.assertIs(ui.tui._run_error, failure)
            await asyncio.wait_for(ui.tui.request_quit(signal=True), 1)
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(ui.task, 1)
            self.assertIs(caught.exception, failure)

    async def test_signal_cancel_failure_closes_required_navigation_without_reopening(self):
        loop = asyncio.get_running_loop()
        old_handler = signal.getsignal(signal.SIGTERM)
        observed = []
        loop.add_signal_handler(signal.SIGTERM, lambda: observed.append('restored'))
        hooks = (Mock(), Mock(), Mock(), Mock(), object(), Mock())
        try:
            async with self.ui(rows=[workspace()], prior_hooks=hooks) as ui:
                key_quit = forced = None
                original_cancel = ui.dialogs.cancel
                try:
                    await self.modal(ui, 'Show archived')
                    self.assertTrue(ui.state.drafts.set(('session', 'held'), 'unsent'))
                    with patch.object(ui.workflows, 'navigate',
                                      wraps=ui.workflows.navigate) as reopened:
                        key_quit = asyncio.create_task(ui.tui.request_quit())
                        await self.modal(ui, 'Quit with unsent')
                        failure = RuntimeError('persistent dialog cancellation failure')
                        followups = []
                        with patch.object(ui.tui, '_schedule_signal',
                                          side_effect=lambda: followups.append(True)), \
                                patch.object(ui.dialogs, 'cancel', side_effect=failure):
                            forced = ui.tui._spawn(ui.tui.request_quit(signal=True), 'signal')
                            done, _ = await asyncio.wait({forced}, timeout=1)
                            self.assertEqual(done, {forced})
                            self.assertTrue(forced.result())
                            await asyncio.sleep(0)
                            self.assertEqual(followups, [])
                            self.assertTrue(ui.tui._force_quit)
                            self.assertIs(ui.tui._run_error, failure)
                            self.assertTrue(key_quit.result())
                            done, _ = await asyncio.wait({ui.task}, timeout=1)
                            self.assertEqual(done, {ui.task})
                            with self.assertRaises(RuntimeError) as caught:
                                ui.task.result()
                            self.assertIs(caught.exception, failure)
                            self.assertIsNone(ui.dialogs.future)
                            self.assertEqual(ui.events, [])
                        self.assertEqual(reopened.await_count, 0)
                finally:
                    # Restore real cancellation before waking any mutant-stalled owner.
                    original_cancel()
                    pending = {task for task in (key_quit, forced, ui.task)
                               if task is not None and not task.done()}
                    if pending:
                        _, pending = await asyncio.wait(pending, timeout=2)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.wait(pending, timeout=1)
                actual = (ui.client.output, ui.client.on_view_change,
                          ui.client.on_workspace, ui.client.on_settings,
                          ui.controller.presentation, ui.controller.on_view_change)
                for restored, original in zip(actual, hooks):
                    self.assertIs(restored, original)
                loop._signal_handlers[signal.SIGTERM]._run()
                self.assertEqual(observed, ['restored'])
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, old_handler)

    async def test_signal_cancel_failure_drains_blocked_handoff_before_checkpoint_and_exit(self):
        async with self.ui(selected=workspace(agents=[agent()])) as ui:
            await self.connected(ui)
            await ui.type_text('unsent')
            ui.release_foreground.clear()
            attach = asyncio.create_task(ui.tui.attach(ui.controller.workspace['agents'][0]))
            key_quit = forced = None
            original_cancel = ui.dialogs.cancel
            try:
                self.assertTrue(await asyncio.wait_for(
                    asyncio.to_thread(ui.foreground_started.wait, 2), 3))
                key_quit = asyncio.create_task(ui.tui.request_quit())
                await self.modal(ui, 'Quit with unsent')
                failure = RuntimeError('persistent dialog cancellation failure')
                followups = []
                with patch.object(ui.tui, '_schedule_signal',
                                  side_effect=lambda: followups.append(True)), \
                        patch.object(ui.dialogs, 'cancel', side_effect=failure):
                    forced = ui.tui._spawn(ui.tui.request_quit(signal=True), 'signal')
                    await asyncio.sleep(0)
                    self.assertFalse(forced.done())
                    self.assertFalse(ui.task.done())
                    self.assertNotIn('checkpoint', ui.events)
                    self.assertNotIn('terminal_restored', ui.events)
                    ui.release_foreground.set()
                    done, _ = await asyncio.wait({forced}, timeout=2)
                    self.assertEqual(done, {forced})
                    self.assertTrue(forced.result())
                    self.assertEqual(followups, [])
                    self.assertTrue(key_quit.result())
                    done, _ = await asyncio.wait({attach}, timeout=1)
                    self.assertEqual(done, {attach})
                    self.assertEqual(attach.result().status, 'completed')
                    done, _ = await asyncio.wait({ui.task}, timeout=1)
                    self.assertEqual(done, {ui.task})
                    with self.assertRaises(RuntimeError) as caught:
                        ui.task.result()
                    self.assertIs(caught.exception, failure)
                self.assertLess(ui.events.index('foreground_finished'),
                                ui.events.index('terminal_restored'))
                self.assertLess(ui.events.index('terminal_restored'),
                                ui.events.index('checkpoint'))
                self.assertLess(ui.events.index('checkpoint'),
                                ui.events.index('receiver_cancel'))
            finally:
                # Release every dependency before bounded draining.
                ui.release_foreground.set()
                original_cancel()
                pending = {task for task in (attach, key_quit, forced, ui.task)
                           if task is not None and not task.done()}
                if pending:
                    _, pending = await asyncio.wait(pending, timeout=3)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.wait(pending, timeout=1)

    async def test_signal_cancel_failure_never_finishes_non_quit_form_with_false(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            form = asyncio.create_task(ui.tui.run_action('rename_session'))
            release = asyncio.Event()
            fake_quit = asyncio.create_task(release.wait())
            forced = None
            try:
                await self.modal(ui, 'Rename session')
                self.assertIs(ui.dialogs.owner, form)
                ui.tui._quit_task = fake_quit
                failure = RuntimeError('non-quit dialog cancellation failure')
                original_finish = ui.dialogs.finish
                with patch.object(ui.dialogs, 'finish', wraps=original_finish) as finish, \
                        patch.object(ui.dialogs, 'cancel', side_effect=failure):
                    forced = ui.tui._spawn(ui.tui.request_quit(signal=True), 'signal')
                    await asyncio.sleep(0)
                    self.assertFalse(forced.done())
                    self.assertIs(ui.tui._run_error, failure)
                    await asyncio.sleep(0)
                    self.assertNotIn((False,), [call.args for call in finish.call_args_list])
                    release.set()
                    self.assertTrue(await asyncio.wait_for(forced, 1))
                ui.tui._quit_task = None
                await asyncio.wait_for(ui.tui.request_quit(signal=True), 1)
                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.wait_for(ui.task, 1)
                self.assertIs(caught.exception, failure)
            finally:
                release.set()
                pending = [task for task in (form, fake_quit, forced) if task is not None]
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 3)

    async def test_first_failure_wins_when_two_workflow_guards_fail(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            first, second = RuntimeError('first local bug'), RuntimeError('second local bug')
            async def fail(error):
                raise error
            tasks = [ui.tui._spawn(ui.tui._guard(fail(error)), 'action') for error in (first, second)]
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(ui.task, 2)
            self.assertIs(caught.exception, first)

    async def test_quit_internal_failure_still_exits_and_restores(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            failure = RuntimeError('quit drain bug')
            with patch.object(ui.controller, 'wait_pending', side_effect=failure):
                await asyncio.wait_for(ui.tui.request_quit(signal=True), 1)
                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.wait_for(asyncio.shield(ui.task), 1)
                self.assertIs(caught.exception, failure)
                self.assertIsNone(ui.client.on_view_change)
                self.assertIsNone(ui.client.websocket)

    async def test_worker_thread_runs_with_one_signal_unavailable_notice(self):
        async def worker():
            async with self.ui(plain=True) as ui:
                await self.connected(ui)
                notices = [text for text in ui.state.notices.lines if 'signal' in text.lower()]
                self.assertEqual(len(notices), 1)
                self.assertIn('main thread', notices[0])
                await ui.key('CtrlQ')
                await asyncio.wait_for(ui.task, 2)
        await asyncio.wait_for(asyncio.to_thread(lambda: asyncio.run(worker())), 5)

    async def test_quit_view_cleanup_failure_still_restores_hooks(self):
        async with self.ui(selected=workspace()) as ui:
            await self.connected(ui)
            failure = RuntimeError('view cleanup bug')
            with patch.object(ui.view, 'hide_help', side_effect=failure):
                await asyncio.wait_for(ui.tui.request_quit(signal=True), 1)
                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.wait_for(asyncio.shield(ui.task), 1)
                self.assertIs(caught.exception, failure)
                self.assertIsNone(ui.controller.presentation)
                self.assertIsNone(ui.client.on_workspace)

    async def test_receiver_failure_drains_real_terminal_before_exit(self):
        async with self.ui(selected=workspace(agents=[agent()]), real_terminal=True) as ui:
            await self.connected(ui)
            ui.release_foreground.clear()
            action = asyncio.create_task(ui.tui.attach(ui.controller.workspace['agents'][0]))
            try:
                self.assertTrue(await asyncio.to_thread(ui.foreground_started.wait, 2))
                self.assertTrue(ui.application._running_in_terminal)
                count = ui.render_count
                failure = RuntimeError('receiver failed during foreground')
                quitting = asyncio.Event()
                finish_quit = ui.tui._finish_quit
                async def observe_quit(preparation):
                    quitting.set()
                    await finish_quit(preparation)
                ui.tui._finish_quit = observe_quit
                refresh = ui.view.refresh
                def fail_message(event=None):
                    if event is not None and event.kind == 'messages':
                        raise failure
                    return refresh(event)
                ui.view.refresh = fail_message
                await ui.socket.frames.put({'type': 'message', 'data': {
                    'id': 334, 'channel': 'ws_one', 'text': 'trigger failure'}})
                await asyncio.wait_for(quitting.wait(), 2)
                self.assertFalse(ui.task.done())
                self.assertTrue(ui.application._running_in_terminal)
                self.assertEqual(ui.render_count, count)
                ui.release_foreground.set()
                await asyncio.wait_for(action, 2)
                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.wait_for(ui.task, 2)
                self.assertIs(caught.exception, failure)
                self.assertFalse(ui.application._running_in_terminal)
                self.assertIsNone(ui.controller.presentation)
                self.assertLess(ui.events.index('foreground_finished'), ui.events.index('checkpoint'))
            finally:
                ui.release_foreground.set()
                await asyncio.gather(action, return_exceptions=True)
