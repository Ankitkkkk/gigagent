"""Shared lifecycle actions: payload, ownership, and legacy parity contracts."""

import asyncio
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from cli import ChatClient
from cli_api import CLIError
from cli_view_contracts import ActionOutcome
from cli_workspaces import WorkspaceAPI, WINDOWS_TMUX_ERROR
from cli_workspace_chat import WorkspaceChatController, SUMMARY_ERROR


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def controller_with_workspace(self, *, tui=True):
        self.output = []
        self.api = Mock(spec=WorkspaceAPI)
        self.agent = {
            'agent_id': 'ag_a', 'provider': 'codex', 'registry_name': 'codex-1',
            'cwd': '/tmp/project', 'native_session_id': 'native-a',
            'tmux_session': 'yapp-ag_a', 'last_state': 'exited',
            'last_error': None, 'last_launch': None, 'unread_count': 0,
            'history_mode': 'literal', 'history_state': 'done', 'history_note': None,
            'previous_native_ids': [], 'previous_cwds': [],
            'floor_id': 0, 'read_mark': 0, 'acked_above_mark': [],
            'native_verified': True, 'joined_at': '2026-09-13T00:00:00Z',
        }
        self.ws = {
            'id': 'ws_a', 'name': 'billing', 'channel': 'ws-a', 'archived': False,
            'created_at': '2026-09-13T00:00:00Z', 'updated_at': '2026-09-13T00:00:00Z',
            'agents': [copy.deepcopy(self.agent)],
        }
        self.api.action.return_value = dict(self.agent, last_state='starting')
        self.api.rename.return_value = dict(self.ws, name='renamed')
        self.api.create.return_value = dict(self.ws, id='ws_new', channel='ws-new', agents=[])
        self.api.unread.return_value = {'agents': []}
        client = ChatClient('http://127.0.0.1:18300', output=self.output.append)
        ctl = WorkspaceChatController(client, self.api)
        ctl._select(copy.deepcopy(self.ws))
        ctl.prompt = AsyncMock(side_effect=AssertionError('unexpected legacy prompt'))
        self.events = []
        self.presenter = Mock(spec=['confirm', 'attach', 'notice'])
        self.presenter.confirm = AsyncMock()
        self.presenter.attach = AsyncMock(return_value=ActionOutcome(
            'completed', workspace_id='ws_a', agent_id='ag_a'))
        if tui:
            ctl.bind_view(self.presenter, self.events.append)
        return ctl

    async def test_remove_on_old_server_explains_restart_and_keeps_entry(self):
        ctl = self.controller_with_workspace()
        self.api.action.side_effect = CLIError('Not Found', 404)
        outcome = await ctl.execute_action('remove', {'agent_id': 'ag_a'})
        self.assertEqual(outcome.status, 'failed')
        self.assertIn('Restart the yapp server', outcome.message)
        self.assertIn('not removed', outcome.message)
        self.assertEqual(ctl.workspace['agents'][0]['agent_id'], 'ag_a')
        self.presenter.notice.assert_called_with(outcome.message)

    async def test_remove_missing_agent_keeps_specific_server_error(self):
        ctl = self.controller_with_workspace()
        self.api.action.side_effect = CLIError('agent not found', 404)
        outcome = await ctl.execute_action('remove', {'agent_id': 'ag_a'})
        self.assertEqual(outcome.status, 'failed')
        self.assertEqual(outcome.message, 'agent not found')

    async def test_resume_uses_one_mutation_and_returns_stable_ids(self):
        ctl = self.controller_with_workspace()
        self.assertTrue(callable(getattr(ctl, 'execute_action', None)), 'execute_action missing')
        outcome = await ctl.execute_action('resume', {
            'agent_id': 'ag_a', 'fresh': False, 'cwd': None, 'name': None})
        self.assertEqual(outcome, ActionOutcome('completed', workspace_id='ws_a', agent_id='ag_a'))
        self.api.action.assert_called_once_with('ws_a', 'resume', 'ag_a',
            body={'fresh': False, 'name': None, 'cwd': None})
        self.assertEqual(ctl.workspace['agents'][0]['last_state'], 'starting')

    async def test_all_action_bodies_and_outcomes(self):
        cases = [
            ('spawn', {'provider': 'codex', 'cwd': '/project space', 'name': None, 'history_mode': 'none'},
             call.action('ws_a', 'spawn', body={'provider': 'codex', 'cwd': '/project space', 'name': None, 'history_mode': 'none'}), 'ws_a', 'ag_a'),
            ('stop', {'agent_id': 'ag_a'}, call.action('ws_a', 'stop', 'ag_a'), 'ws_a', 'ag_a'),
            ('retry', {'agent_id': 'ag_a'}, call.action('ws_a', 'retry', 'ag_a'), 'ws_a', 'ag_a'),
            ('history', {'agent_id': 'ag_a', 'mode': 'none'}, call.action('ws_a', 'history', 'ag_a', body={'mode': 'none'}), 'ws_a', 'ag_a'),
            ('unread', {}, call.unread('ws_a', None), 'ws_a', None),
            ('unread', {'agent_id': 'ag_a'}, call.unread('ws_a', 'ag_a'), 'ws_a', 'ag_a'),
            ('create_session', {'name': ''}, call.create(''), 'ws_new', None),
            ('rename_session', {'name': 'renamed'}, call.rename('ws_a', 'renamed'), 'ws_a', None),
            ('archive_session', {'confirmed': True}, call.action('ws_a', 'archive'), 'ws_a', None),
        ]
        for action, payload, expected, ws_id, agent_id in cases:
            with self.subTest(action=action, payload=payload):
                ctl = self.controller_with_workspace()
                result = await ctl.execute_action(action, payload)
                self.assertEqual(result, ActionOutcome('completed', workspace_id=ws_id, agent_id=agent_id))
                self.assertEqual(self.api.mock_calls, [expected])
                ctl.prompt.assert_not_awaited()

    async def test_rename_emits_once_after_revision_and_state_change(self):
        ctl = self.controller_with_workspace()
        before_revision = ctl._state_revision
        before_generation = ctl._selection_version
        observed = []
        def observe(event):
            self.assertEqual(ctl.workspace['name'], 'renamed')
            self.assertEqual(ctl._state_revision, before_revision + 1)
            self.assertEqual(event.revision, before_revision + 1)
            self.assertEqual(event.selection_generation, before_generation)
            observed.append(event)
        ctl.bind_view(self.presenter, observe)
        await ctl.execute_action('rename_session', {'name': 'renamed'})
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].workspace_id, 'ws_a')

    async def test_invalid_payloads_fail_before_api_or_prompt(self):
        cases = [('rename', {'name': 'x'}), ('select_session', {'session_id': ''}),
                 ('resume', {'agent_id': 'ag_a'}), ('stop', {'agent_id': 'ag_a', 'extra': True}),
                 ('stop', {'agent_id': 'codex'}), ('stop', {'agent_id': 'ag_other'}),
                 ('stop', {'agent_id': None}), ('archive_session', {'confirmed': 'yes'}),
                 ('spawn', {'provider': '', 'cwd': None, 'name': None, 'history_mode': 'literal'}),
                 ('spawn', {'provider': 'codex', 'cwd': None, 'name': None, 'history_mode': 'literal'}),
                 ('spawn', {'provider': 'codex', 'cwd': '', 'name': None, 'history_mode': 'literal'}),
                 ('resume', {'agent_id': 'ag_a', 'fresh': 'yes', 'cwd': None, 'name': None}),
                 ('history', {'agent_id': 'ag_a', 'mode': 'summary'}), ('unread', []),
                 ('create_session', {'name': None})]
        for action, payload in cases:
            with self.subTest(action=action, payload=payload):
                ctl = self.controller_with_workspace()
                result = await ctl.execute_action(action, payload)
                self.assertEqual(result.status, 'failed')
                self.assertTrue(result.message)
                self.assertEqual(self.api.mock_calls, [])
                ctl.prompt.assert_not_awaited()

    async def test_summary_and_windows_refuse_with_exact_notice(self):
        ctl = self.controller_with_workspace()
        outcome = await ctl.execute_action('history', {'agent_id': 'ag_a', 'mode': 'summary'})
        self.assertEqual(outcome.message, SUMMARY_ERROR)
        with patch('cli_workspaces.os.name', 'nt'):
            for action, payload in [('resume', {'agent_id': 'ag_a', 'fresh': False, 'cwd': None, 'name': None}),
                                    ('spawn', {'provider': 'codex', 'cwd': '/tmp/project', 'name': None, 'history_mode': 'literal'}),
                                    ('attach', {'agent_id': 'ag_a'})]:
                outcome = await ctl.execute_action(action, payload)
                self.assertEqual(outcome.message, WINDOWS_TMUX_ERROR)
        self.assertEqual(self.api.mock_calls, [])
        self.presenter.attach.assert_not_awaited()

    async def test_declined_archive_performs_no_mutation(self):
        ctl = self.controller_with_workspace()
        self.assertEqual(await ctl.execute_action('archive_session', {'confirmed': False}),
                         ActionOutcome('cancelled', workspace_id='ws_a'))
        self.assertEqual(self.api.mock_calls, [])

    async def test_expected_errors_fail_safely_but_local_errors_propagate(self):
        for error, expected in [(CLIError('refused\x1b[2J'), 'refused[2J'),
                                (OSError('http://localhost?token=SECRET'), 'Session request failed or timed out. Check the local server and retry.'),
                                (TimeoutError('SECRET'), 'Session request failed or timed out. Check the local server and retry.')]:
            with self.subTest(error=error):
                ctl = self.controller_with_workspace()
                self.api.action.side_effect = error
                outcome = await ctl.execute_action('stop', {'agent_id': 'ag_a'})
                self.assertEqual(outcome, ActionOutcome('failed', expected, 'ws_a', 'ag_a'))
                self.presenter.notice.assert_called_once_with(expected)
        self.api.action.side_effect = ValueError('local bug')
        with self.assertRaisesRegex(ValueError, 'local bug'):
            await ctl.execute_action('stop', {'agent_id': 'ag_a'})

    async def test_tui_attach_uses_presenter_without_legacy_pause(self):
        ctl = self.controller_with_workspace()
        with patch.object(ctl.client, 'pause_output') as pause, patch('cli_workspace_chat.attach_agent') as helper:
            outcome = await ctl.execute_action('attach', {'agent_id': 'ag_a'})
        self.assertEqual(outcome, ActionOutcome('completed', workspace_id='ws_a', agent_id='ag_a'))
        self.presenter.attach.assert_awaited_once_with(self.agent)
        pause.assert_not_called()
        helper.assert_not_called()

    async def test_dispatch_delegates_before_parsing_unrelated_or_tui_commands(self):
        ctl = self.controller_with_workspace()
        for text in ['hello "', '/unknown "', '/history', '/sessions "', '/archive "', '/quit', '/exit', '/help', '/agents']:
            self.assertIsNone(await ctl.dispatch_action(text), text)
        self.assertEqual(self.api.mock_calls, [])

    async def test_dispatch_resolves_aliases_and_reports_parse_failure(self):
        ctl = self.controller_with_workspace()
        result = await ctl.dispatch_action('/resume codex --fresh --cwd "/project space" --agent-name "new name"')
        self.assertEqual(result.status, 'completed')
        self.api.action.assert_called_once_with('ws_a', 'resume', 'ag_a', body={
            'fresh': True, 'cwd': '/project space', 'name': 'new name'})
        result = await ctl.dispatch_action('/stop "')
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.message, 'No closing quotation')

    async def test_prompt_values_are_gathered_without_action_lock(self):
        ctl = self.controller_with_workspace(tui=False)
        async def prompt(text, default=''):
            self.assertFalse(ctl._action_lock.locked())
            await ctl.execute_action('retry', {'agent_id': 'ag_a'})
            return default
        ctl.prompt = prompt
        self.assertEqual(await ctl.handle('/spawn codex'), 'continue')
        self.api.action.assert_called_with('ws_a', 'spawn', body={
            'provider': 'codex', 'cwd': '/tmp/project', 'name': None, 'history_mode': 'literal'})

    async def test_overlap_preserves_event_without_replaying_mutation(self):
        ctl = self.controller_with_workspace()
        entered, release = threading.Event(), threading.Event()
        def action(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('response barrier timed out')
            return dict(self.agent, last_state='starting')
        self.api.action.side_effect = action
        pending = asyncio.create_task(ctl.execute_action('resume', {
            'agent_id': 'ag_a', 'fresh': False, 'cwd': None, 'name': None}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            revision = ctl._state_revision
            generation = ctl._selection_version
            def observe(event):
                self.assertEqual(ctl.workspace['agents'][0]['last_state'], 'running')
                self.assertEqual(event.revision, revision + 1)
                self.assertEqual(event.selection_generation, generation)
            ctl.bind_view(self.presenter, observe)
            ctl.client.handle_event({'type': 'workspace', 'data': dict(self.ws, agents=[dict(self.agent, last_state='running')])})
            release.set()
            self.assertEqual((await pending).status, 'completed')
            await ctl.wait_pending()
            self.assertEqual(ctl.workspace['agents'][0]['last_state'], 'running')
            self.assertTrue(ctl._refresh_requested)
            self.api.action.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_cancelled_caller_waits_for_mutation_and_state_commit(self):
        for error in (None, CLIError('refused')):
            with self.subTest(error=error):
                ctl = self.controller_with_workspace()
                entered, release = threading.Event(), threading.Event()
                def action(*args, **kwargs):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('response barrier timed out')
                    if error:
                        raise error
                    return dict(self.agent, last_state='starting')
                self.api.action.side_effect = action
                pending = asyncio.create_task(ctl.execute_action('stop', {'agent_id': 'ag_a'}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    pending.cancel()
                    checkpoint = asyncio.get_running_loop().create_future()
                    asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
                    await checkpoint
                    self.assertFalse(pending.done())
                    waiter = asyncio.create_task(ctl.wait_pending())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                    await waiter
                    self.api.action.assert_called_once()
                    if error is None:
                        self.assertEqual(ctl.workspace['agents'][0]['last_state'], 'starting')
                    else:
                        self.presenter.notice.assert_called_once_with('refused')
                finally:
                    release.set()
                    await asyncio.gather(pending, return_exceptions=True)


    async def test_duplicate_is_cancelled_distinct_actions_serialize_and_key_expires(self):
        ctl = self.controller_with_workspace()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def action(ws_id, action, *args, **kwargs):
            calls.append(action)
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('response barrier timed out')
            return dict(self.agent, last_state='starting')
        self.api.action.side_effect = action
        first = asyncio.create_task(ctl.execute_action('stop', {'agent_id': 'ag_a'}))
        other = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertTrue(ctl._action_lock.locked())
            duplicate = await ctl.execute_action('stop', {'agent_id': 'ag_a'})
            self.assertEqual(duplicate.status, 'cancelled')
            self.assertIn('progress', duplicate.message)
            other = asyncio.create_task(ctl.execute_action('history', {'agent_id': 'ag_a', 'mode': 'none'}))
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            self.assertEqual(calls, ['stop'])
            self.assertFalse(other.done())
            first.cancel()
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            duplicate = await ctl.execute_action('stop', {'agent_id': 'ag_a'})
            self.assertEqual(duplicate.status, 'cancelled')
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual((await other).status, 'completed')
            self.assertEqual((await ctl.execute_action('stop', {'agent_id': 'ag_a'})).status, 'completed')
            self.assertEqual((await ctl.execute_action('stop', {'agent_id': 'ag_a'})).status, 'completed')
            self.assertEqual(calls, ['stop', 'history', 'stop', 'stop'])
        finally:
            release.set()
            await asyncio.gather(*[t for t in (first, other) if t], return_exceptions=True)

    async def test_archive_prompt_does_not_block_other_action(self):
        ctl = self.controller_with_workspace(tui=False)
        opened = asyncio.Event()
        answer = asyncio.get_running_loop().create_future()
        async def prompt(text, default=''):
            self.assertEqual(text, 'Archive session? [y/N]')
            self.assertFalse(ctl._action_lock.locked())
            opened.set()
            return await answer
        ctl.prompt = prompt
        archive = asyncio.create_task(ctl.handle('/archive'))
        try:
            await opened.wait()
            self.assertEqual((await ctl.execute_action('retry', {'agent_id': 'ag_a'})).status, 'completed')
            answer.set_result('n')
            self.assertEqual(await archive, 'continue')
            self.api.action.assert_called_once_with('ws_a', 'retry', 'ag_a')
        finally:
            archive.cancel()
            await asyncio.gather(archive, return_exceptions=True)


    async def test_tui_spawn_defaults_are_explicit_and_never_prompt(self):
        ctl = self.controller_with_workspace()
        result = await ctl.dispatch_action('/spawn codex')
        self.assertEqual(result.status, 'completed')
        self.api.action.assert_called_once_with('ws_a', 'spawn', body={
            'provider': 'codex', 'cwd': '/tmp/project', 'name': None, 'history_mode': 'literal'})
        self.assertIn('/tmp/project', result.message)
        self.assertIn('literal', result.message)
        self.assertIn('--cwd', result.message)
        self.assertIn('--history-mode', result.message)
        self.presenter.notice.assert_called_once_with(result.message)
        self.presenter.confirm.assert_not_called()
        ctl.prompt.assert_not_awaited()
        self.presenter.reset_mock()
        result = await ctl.dispatch_action('/spawn codex --cwd /explicit --history-mode none')
        self.assertIsNone(result.message)
        self.presenter.notice.assert_not_called()
        ctl.workspace['agents'] = []
        with patch('cli_workspace_chat.Path.cwd', return_value=Path('/fallback')):
            result = await ctl.dispatch_action('/spawn codex --history-mode none')
        self.assertIn('/fallback', result.message)
        self.assertIn('none', result.message)
        self.api.action.assert_called_with('ws_a', 'spawn', body={
            'provider': 'codex', 'cwd': '/fallback', 'name': None, 'history_mode': 'none'})

    async def test_resume_hint_retains_entered_selector_and_flags(self):
        for selector in ('ag_a', 'codex-1', 'codex'):
            with self.subTest(selector=selector):
                ctl = self.controller_with_workspace(tui=False)
                self.api.action.side_effect = CLIError('native session id unknown; use --fresh', 409)
                await ctl.handle('/resume ' + selector + ' --cwd /project --agent-name reviewer')
                self.assertEqual(self.output[-1], '/resume ' + selector + ' --fresh --agent-name reviewer --cwd /project')

    async def test_selection_changed_before_owned_task_starts_cannot_accept_old_snapshot(self):
        ctl = self.controller_with_workspace()
        replacement = dict(self.ws, id='ws_b', channel='ws-b', name='other')
        loop = asyncio.get_running_loop()
        # execute_action admits work without yielding; its owned task is scheduled
        # after this callback. A later selection must still reject its old result.
        loop.call_soon(ctl._select, replacement)
        result = await ctl.execute_action('stop', {'agent_id': 'ag_a'})
        self.assertEqual(result.workspace_id, 'ws_a')
        self.assertEqual(ctl.workspace, replacement)
        self.api.action.assert_called_once_with('ws_a', 'stop', 'ag_a')

    async def test_create_without_selection_returns_id_without_selecting(self):
        ctl = self.controller_with_workspace()
        ctl._select(None)
        ctl._closed = True
        before = ctl._selection_version
        result = await ctl.execute_action('create_session', {'name': ''})
        self.assertEqual(result, ActionOutcome('completed', workspace_id='ws_new'))
        self.assertIsNone(ctl.workspace)
        self.assertEqual(ctl._selection_version, before)

    async def test_cancelled_queued_action_is_not_sent_and_can_be_reactivated(self):
        ctl = self.controller_with_workspace()
        await ctl._action_lock.acquire()
        queued = asyncio.create_task(ctl.execute_action('stop', {'agent_id': 'ag_a'}))
        try:
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            self.api.action.assert_not_called()
        finally:
            ctl._action_lock.release()
        self.assertEqual((await ctl.execute_action('stop', {'agent_id': 'ag_a'})).status, 'completed')
        self.api.action.assert_called_once_with('ws_a', 'stop', 'ag_a')

    async def test_attach_cancellation_keeps_lock_until_presenter_finishes(self):
        ctl = self.controller_with_workspace()
        started = asyncio.Event()
        release = asyncio.get_running_loop().create_future()
        async def attach(agent):
            started.set()
            await release
            return ActionOutcome('completed', workspace_id='ws_a', agent_id='ag_a')
        self.presenter.attach.side_effect = attach
        pending = asyncio.create_task(ctl.execute_action('attach', {'agent_id': 'ag_a'}))
        try:
            await started.wait()
            pending.cancel()
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            self.assertFalse(pending.done())
            self.assertTrue(ctl._action_lock.locked())
            self.assertEqual((await ctl.execute_action('attach', {'agent_id': 'ag_a'})).status, 'cancelled')
            release.set_result(None)
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await ctl.wait_pending()
            self.assertFalse(ctl._action_lock.locked())
        finally:
            if not release.done():
                release.set_result(None)
            await asyncio.gather(pending, return_exceptions=True)

    async def test_direct_mutation_drains_repeated_cancellation_and_consumes_error(self):
        ctl = self.controller_with_workspace()
        entered, release = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        unhandled = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        def mutate():
            entered.set()
            if not release.wait(3):
                raise AssertionError('response barrier timed out')
            raise CLIError('completed refusal')
        pending = asyncio.create_task(ctl._run_mutation(mutate))
        waiter = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            for _ in range(2):
                pending.cancel()
                barrier = loop.create_future()
                loop.call_soon(barrier.set_result, None)
                await barrier
                self.assertFalse(pending.done())
            waiter = asyncio.create_task(ctl.wait_pending())
            barrier = loop.create_future()
            loop.call_soon(barrier.set_result, None)
            await barrier
            self.assertFalse(waiter.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await waiter
            self.assertEqual(unhandled, [])
            self.assertFalse(ctl._pending_mutations)
        finally:
            release.set()
            await asyncio.gather(*[t for t in (pending, waiter) if t], return_exceptions=True)
            loop.set_exception_handler(previous)

    async def test_queued_action_revalidates_selection_and_agent_membership(self):
        for changed_selection in (True, False):
            with self.subTest(changed_selection=changed_selection):
                ctl = self.controller_with_workspace()
                await ctl._action_lock.acquire()
                pending = asyncio.create_task(ctl.execute_action('stop', {'agent_id': 'ag_a'}))
                try:
                    barrier = asyncio.get_running_loop().create_future()
                    asyncio.get_running_loop().call_soon(barrier.set_result, None)
                    await barrier
                    if changed_selection:
                        ctl._select(copy.deepcopy(self.ws))
                    else:
                        ctl.on_workspace(dict(self.ws, agents=[]))
                finally:
                    ctl._action_lock.release()
                outcome = await pending
                self.assertEqual(outcome.status, 'cancelled' if changed_selection else 'failed')
                self.api.action.assert_not_called()

    async def test_prompt_selection_change_cancels_without_mutating_new_session(self):
        ctl = self.controller_with_workspace(tui=False)
        async def prompt(text, default=''):
            ctl._select(dict(self.ws, id='ws_b', channel='ws-b'))
            return default
        ctl.prompt = prompt
        result = await ctl.dispatch_action('/spawn codex')
        self.assertEqual(result, ActionOutcome('cancelled', workspace_id='ws_a'))
        self.api.action.assert_not_called()


    async def test_legacy_multiline_error_preserves_lines_and_removes_controls(self):
        ctl = self.controller_with_workspace(tui=False)
        self.api.action.side_effect = CLIError('first line\nsecond line\x1b[2J')
        outcome = await ctl.dispatch_action('/stop codex')
        self.assertEqual(outcome.status, 'failed')
        self.assertEqual(outcome.message, 'first line\nsecond line[2J')
        self.assertEqual(self.output, ['first line\nsecond line[2J'])

    async def test_optional_unread_payloads_share_admission_before_lock(self):
        ctl = self.controller_with_workspace()
        await ctl._action_lock.acquire()
        first = asyncio.create_task(ctl.execute_action('unread', {}))
        second = None
        try:
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            second = asyncio.create_task(ctl.execute_action('unread', {'agent_id': None}))
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            self.assertTrue(second.done())
            self.assertEqual(second.result().status, 'cancelled')
            self.assertFalse(first.done())
            self.api.unread.assert_not_called()
        finally:
            ctl._action_lock.release()
            await asyncio.gather(*[task for task in (first, second) if task], return_exceptions=True)
        self.assertEqual(first.result().status, 'completed')
        self.api.unread.assert_called_once_with('ws_a', None)

    async def test_identical_callers_queued_behind_lock_are_deduplicated(self):
        ctl = self.controller_with_workspace()
        await ctl._action_lock.acquire()
        first = asyncio.create_task(ctl.execute_action('stop', {'agent_id': 'ag_a'}))
        second = None
        try:
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            second = asyncio.create_task(ctl.execute_action('stop', {'agent_id': 'ag_a'}))
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            self.assertTrue(second.done())
            self.assertEqual(second.result().status, 'cancelled')
            self.assertFalse(first.done())
            self.api.action.assert_not_called()
        finally:
            ctl._action_lock.release()
            await asyncio.gather(*[task for task in (first, second) if task], return_exceptions=True)
        self.assertEqual(first.result().status, 'completed')
        self.api.action.assert_called_once_with('ws_a', 'stop', 'ag_a')

    async def test_same_action_different_payloads_are_both_executed(self):
        ctl = self.controller_with_workspace()
        await ctl._action_lock.acquire()
        first = asyncio.create_task(ctl.execute_action('history', {'agent_id': 'ag_a', 'mode': 'none'}))
        second = asyncio.create_task(ctl.execute_action('history', {'agent_id': 'ag_a', 'mode': 'literal'}))
        try:
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            self.api.action.assert_not_called()
        finally:
            ctl._action_lock.release()
            await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(first.result().status, 'completed')
        self.assertEqual(second.result().status, 'completed')
        self.assertEqual(self.api.mock_calls, [
            call.action('ws_a', 'history', 'ag_a', body={'mode': 'none'}),
            call.action('ws_a', 'history', 'ag_a', body={'mode': 'literal'})])

    async def test_noncreate_action_without_selection_fails_before_api(self):
        ctl = self.controller_with_workspace()
        ctl._select(None)
        outcome = await ctl.execute_action('stop', {'agent_id': 'ag_a'})
        self.assertEqual(outcome.status, 'failed')
        self.assertIsNone(outcome.workspace_id)
        self.assertIn('requires a selected session', outcome.message)
        self.assertEqual(self.api.mock_calls, [])
        ctl.prompt.assert_not_awaited()
