"""Transactional selection boundaries, cancellation, and archive state."""

import asyncio
import copy
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from cli import ChatClient
from cli_api import CLIError
from cli_view_contracts import ActionOutcome
from cli_workspaces import WorkspaceAPI, WINDOWS_TMUX_ERROR
from cli_workspace_chat import WorkspaceChatController


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    def make_controller(self, *, old=True, stopped=False, archived=False, no_resume=False):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.agent = {
            'agent_id': 'ag_b', 'provider': 'codex', 'registry_name': 'codex-2',
            'cwd': self.directory.name, 'native_session_id': 'native-b',
            'tmux_session': 'agentchattr-ag_b', 'last_state': 'exited',
            'last_error': None, 'last_launch': None, 'unread_count': 0,
            'history_mode': 'literal', 'history_state': 'done', 'history_note': None,
            'previous_native_ids': [], 'previous_cwds': [], 'floor_id': 0,
            'read_mark': 0, 'acked_above_mark': [], 'native_verified': True,
            'joined_at': '2026-09-13T00:00:00Z',
        }
        self.old = {
            'id': 'ws_a', 'name': 'old', 'channel': 'session-a', 'archived': False,
            'created_at': '2026-09-13T00:00:00Z', 'updated_at': '2026-09-13T00:00:00Z',
            'agents': [],
        }
        self.candidate = dict(self.old, id='ws_b', name='candidate', channel='session-b',
                              archived=archived, agents=[copy.deepcopy(self.agent)] if stopped else [])
        self.output, self.trace, self.events = [], [], []
        self.api = Mock(spec=WorkspaceAPI)
        self.api.list.return_value = {'workspaces': [self.old, self.candidate]}
        def get(ws_id):
            self.trace.append(('get', ws_id))
            return copy.deepcopy(self.old if ws_id == 'ws_a' else self.candidate)
        def action(ws_id, action, agent_id=None, body=None):
            self.trace.append((action, ws_id))
            if action == 'unarchive':
                self.candidate['archived'] = False
                return copy.deepcopy(self.candidate)
            if action == 'resume':
                self.candidate['agents'][0]['last_state'] = 'starting'
                return copy.deepcopy(self.candidate['agents'][0])
            return copy.deepcopy(self.old)
        self.api.get.side_effect = get
        self.api.action.side_effect = action
        client = ChatClient('http://127.0.0.1:18300', output=self.output.append)
        ctl = WorkspaceChatController(client, self.api, no_resume=no_resume)
        if old:
            ctl._select(copy.deepcopy(self.old))
        self.presenter = Mock(spec=['confirm', 'attach', 'notice'])
        self.dialog_open, self.dialog_release = asyncio.Event(), asyncio.Event()
        self.choice = False
        async def confirm(text, *, default=False, escape=False):
            self.assertFalse(ctl._action_lock.locked())
            self.trace.append(('dialog', text, default, escape))
            self.dialog_open.set()
            await self.dialog_release.wait()
            return self.choice
        self.presenter.confirm = AsyncMock(side_effect=confirm)
        ctl.bind_view(self.presenter, self.events.append)
        self.addAsyncCleanup(ctl.cancel_selection) if hasattr(ctl, 'cancel_selection') else None
        return ctl

    async def tick(self):
        barrier = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(barrier.set_result, None)
        await barrier

    def assert_clean(self, ctl):
        self.assertFalse(ctl.selection_pending)
        self.assertIsNone(ctl._selection_task)
        self.assertIsNone(ctl._selection_commit)
        self.assertFalse(ctl._pending_mutations)
        self.assertFalse(ctl._pending_actions)
        self.assertFalse(ctl._action_lock.locked())

    async def test_cancel_dialog_preserves_old_selection(self):
        ctl = self.make_controller(stopped=True)
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        ctl._failed_launches.add('ag_old')
        ctl._agent_states = {'ag_old': 'failed'}
        ctl._refresh_requested = False
        ctl._poll_error = 'old failure'
        before = (ctl.workspace, ctl.client.channel, ctl._closed, ctl._selection_version,
                  ctl._state_revision, set(ctl._failed_launches), dict(ctl._agent_states),
                  ctl._refresh_requested, ctl._poll_error, ctl.client.pending_channel)
        task = asyncio.create_task(ctl.select_session('ws_b'))
        await asyncio.wait_for(self.dialog_open.wait(), 2)
        self.assertTrue(ctl.selection_pending)
        await ctl.cancel_selection()
        self.assertEqual((await task).status, 'cancelled')
        self.assertEqual((ctl.workspace, ctl.client.channel, ctl._closed, ctl._selection_version,
                          ctl._state_revision, ctl._failed_launches, ctl._agent_states,
                          ctl._refresh_requested, ctl._poll_error, ctl.client.pending_channel), before)
        self.api.action.assert_not_called()
        self.assertEqual(self.events, [])
        self.assert_clean(ctl)

    async def test_first_commit_notifies_only_after_all_state_changes(self):
        ctl = self.make_controller(old=False)
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        generation, revision = ctl._selection_version, ctl._state_revision
        def observe(event):
            self.assertEqual(event.kind, 'selection')
            self.assertEqual(event.workspace_id, 'ws_b')
            self.assertEqual(event.selection_generation, generation + 1)
            self.assertEqual(event.revision, revision)
            self.assertEqual(ctl.workspace, self.candidate)
            self.assertEqual(ctl.client.channel, 'session-b')
            self.assertIsNone(ctl.client.pending_channel)
            self.assertFalse(ctl._closed)
            self.assertTrue(ctl._refresh_requested)
            self.trace.append(('selection', event.workspace_id))
        ctl.on_view_change = observe
        self.assertEqual(await ctl.execute_action('select_session', {'session_id': 'ws_b'}),
                         ActionOutcome('completed', workspace_id='ws_b'))
        self.assertEqual(self.trace, [('get', 'ws_b'), ('selection', 'ws_b')])
        self.api.action.assert_not_called()
        self.assert_clean(ctl)

    async def test_same_selection_preserves_failed_launches_without_reads(self):
        ctl = self.make_controller()
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        ctl._failed_launches.add('ag_a')
        generation = ctl._selection_version
        self.assertEqual(await ctl.select_session('ws_a'), ActionOutcome('completed', workspace_id='ws_a'))
        self.assertEqual(ctl._failed_launches, {'ag_a'})
        self.assertEqual(ctl._selection_version, generation)
        self.assertEqual(self.api.mock_calls, [])
        self.assertEqual(self.events, [])
        self.assert_clean(ctl)

    async def test_archive_notifies_closed_no_selection_and_quit_does_not_checkpoint(self):
        ctl = self.make_controller()
        generation, revision = ctl._selection_version, ctl._state_revision
        def observe(event):
            self.assertEqual(event.kind, 'selection')
            self.assertIsNone(event.workspace_id)
            self.assertIsNone(ctl.workspace)
            self.assertTrue(ctl._closed)
            self.assertFalse(ctl._refresh_requested)
            self.assertEqual(ctl._agent_states, {})
            self.assertEqual(event.selection_generation, generation + 1)
            self.assertEqual(event.revision, revision)
            self.events.append(event)
        ctl.on_view_change = observe
        result = await ctl.execute_action('archive_session', {'confirmed': True})
        await ctl.close()
        self.assertEqual(result, ActionOutcome('completed', workspace_id='ws_a'))
        self.assertEqual(len(self.events), 1)
        self.api.action.assert_called_once_with('ws_a', 'archive')

    async def test_archived_decline_is_cancelled_without_checkpoint(self):
        ctl = self.make_controller(archived=True)
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        self.dialog_release.set()
        result = await ctl.select_session('ws_b')
        self.assertEqual(result.status, 'cancelled')
        self.presenter.confirm.assert_awaited_once_with('Unarchive it? [y/N]', default=False, escape=False)
        self.assertEqual(ctl.workspace, self.old)
        self.api.action.assert_not_called()
        self.assert_clean(ctl)

    async def test_batch_default_yes_and_escape_chat_only(self):
        for answer, calls in [(True, [call('ws_b', 'resume', 'ag_b', body={}), call('ws_a', 'checkpoint')]),
                              (False, [call('ws_a', 'checkpoint')])]:
            with self.subTest(answer=answer):
                ctl = self.make_controller(stopped=True)
                self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
                self.choice = answer
                self.dialog_release.set()
                result = await ctl.select_session('ws_b')
                self.assertEqual(result.status, 'completed')
                self.presenter.confirm.assert_awaited_once_with('Resume 1 stopped agents? [Y/n]', default=True, escape=False)
                self.assertEqual(self.api.action.call_args_list, calls)
                self.assertEqual(ctl.workspace['agents'][0]['last_state'], 'starting' if answer else 'exited')
                self.assert_clean(ctl)

    async def test_no_resume_and_missing_cwd_skip_dialog_and_resume(self):
        for no_resume in (True, False):
            with self.subTest(no_resume=no_resume):
                ctl = self.make_controller(stopped=True, no_resume=no_resume)
                self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
                if not no_resume:
                    self.candidate['agents'][0]['cwd'] = self.directory.name + '/missing'
                self.assertEqual((await ctl.select_session('ws_b')).status, 'completed')
                self.presenter.confirm.assert_not_awaited()
                self.api.action.assert_called_once_with('ws_a', 'checkpoint')
                self.assert_clean(ctl)

    async def test_windows_retains_chat_only_notice(self):
        ctl = self.make_controller(stopped=True)
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        with patch('cli_workspace_chat.sys.platform', 'win32'):
            self.assertEqual((await ctl.select_session('ws_b')).status, 'completed')
        self.presenter.confirm.assert_not_awaited()
        self.presenter.notice.assert_any_call(WINDOWS_TMUX_ERROR)
        self.api.action.assert_called_once_with('ws_a', 'checkpoint')

    async def test_precommit_read_and_unarchive_failures_preserve_old_selection(self):
        for unarchive in (False, True):
            with self.subTest(unarchive=unarchive):
                ctl = self.make_controller(archived=unarchive)
                self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
                if unarchive:
                    self.choice = True
                    self.dialog_release.set()
                    self.api.action.side_effect = CLIError('unarchive refused', 409)
                else:
                    self.api.get.side_effect = CLIError('read refused', 404)
                result = await ctl.select_session('ws_b')
                self.assertEqual(result.status, 'failed')
                self.assertEqual(ctl.workspace, self.old)
                self.assertFalse(ctl._closed)
                self.assertEqual(ctl.client.channel, 'session-a')
                self.assertNotIn(call('ws_a', 'checkpoint'), self.api.action.call_args_list)
                self.assert_clean(ctl)

    async def test_quit_waits_checkpoint_blocked_commit_then_checkpoints_new_selection(self):
        ctl = self.make_controller()
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        entered, release = threading.Event(), threading.Event()
        action = self.api.action.side_effect
        def checkpoint(ws_id, name, *args, **kwargs):
            if ws_id == 'ws_a':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('checkpoint barrier timed out')
            return action(ws_id, name, *args, **kwargs)
        self.api.action.side_effect = checkpoint
        selecting = asyncio.create_task(ctl.select_session('ws_b'))
        quitting = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            commit = ctl._selection_commit
            self.assertIsNotNone(commit)
            async def quit_sequence():
                await ctl.cancel_selection()
                await ctl.wait_pending()
                await ctl.close()
            quitting = asyncio.create_task(quit_sequence())
            await self.tick()
            self.assertFalse(quitting.done())
            self.assertFalse(commit.cancelled())
            self.assertEqual(ctl.workspace['id'], 'ws_a')
            release.set()
            self.assertEqual((await selecting).status, 'completed')
            await quitting
            self.assertEqual(ctl.workspace['id'], 'ws_b')
            self.assertTrue(ctl._closed)
            self.assertEqual(self.api.action.call_args_list, [call('ws_a', 'checkpoint'), call('ws_b', 'checkpoint')])
            self.assert_clean(ctl)
        finally:
            release.set()
            await asyncio.gather(*[t for t in (selecting, quitting) if t], return_exceptions=True)

    async def test_list_sessions_returns_rows_without_selection_or_checkpoint(self):
        ctl = self.make_controller()
        self.assertTrue(callable(getattr(ctl, 'list_sessions', None)), 'list_sessions missing')
        self.assertEqual(await ctl.list_sessions(), [self.old, self.candidate])
        self.assertEqual(await ctl.list_sessions(True), [self.old, self.candidate])
        self.assertEqual(self.api.list.call_args_list, [call(include_archived=False), call(include_archived=True)])
        self.assertEqual(ctl.workspace, self.old)
        self.api.action.assert_not_called()
        self.assertEqual(self.events, [])

    async def test_stale_candidate_read_cannot_replace_newer_selection_or_revision(self):
        for selection_change in (True, False):
            with self.subTest(selection_change=selection_change):
                ctl = self.make_controller()
                self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
                entered, release = threading.Event(), threading.Event()
                def get(ws_id):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('read barrier timed out')
                    return copy.deepcopy(self.candidate)
                self.api.get.side_effect = get
                selecting = asyncio.create_task(ctl.select_session('ws_b'))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    changed = dict(self.old, name='newer')
                    if selection_change:
                        ctl._select(changed)
                    else:
                        ctl.on_workspace(changed)
                    release.set()
                    self.assertEqual((await selecting).status, 'cancelled')
                    self.assertEqual(ctl.workspace, changed)
                    self.api.action.assert_not_called()
                    self.assert_clean(ctl)
                finally:
                    release.set()
                    await asyncio.gather(selecting, return_exceptions=True)

    async def test_cancelled_thread_read_is_ignored_after_next_selection(self):
        ctl = self.make_controller()
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def get(ws_id):
            entered.set()
            try:
                if not release.wait(3):
                    raise AssertionError('read barrier timed out')
                return copy.deepcopy(self.candidate)
            finally:
                finished.set()
        self.api.get.side_effect = get
        selecting = asyncio.create_task(ctl.select_session('ws_b'))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await ctl.cancel_selection()
            self.assertEqual((await selecting).status, 'cancelled')
            self.assertEqual(ctl.workspace, self.old)
            replacement = dict(self.old, id='ws_c', channel='session-c')
            ctl._select(replacement)
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 2))
            await self.tick()
            self.assertEqual(ctl.workspace, replacement)
            self.api.action.assert_not_called()
            self.assert_clean(ctl)
        finally:
            release.set()
            await asyncio.gather(selecting, return_exceptions=True)

    async def test_cancelled_candidate_mutation_drains_without_rollback_or_commit(self):
        ctl = self.make_controller(archived=True)
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        self.choice = True
        self.dialog_release.set()
        entered, release = threading.Event(), threading.Event()
        action = self.api.action.side_effect
        def unarchive(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('mutation barrier timed out')
            return action(*args, **kwargs)
        self.api.action.side_effect = unarchive
        selecting = asyncio.create_task(ctl.select_session('ws_b'))
        cancelling = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertTrue(ctl._pending_mutations)
            cancelling = asyncio.create_task(ctl.cancel_selection())
            await self.tick()
            self.assertFalse(cancelling.done())
            self.assertEqual(ctl.workspace, self.old)
            release.set()
            await cancelling
            self.assertEqual((await selecting).status, 'cancelled')
            self.assertFalse(self.candidate['archived'])
            self.assertEqual(ctl.workspace, self.old)
            self.api.action.assert_called_once_with('ws_b', 'unarchive')
            self.assert_clean(ctl)
        finally:
            release.set()
            await asyncio.gather(*[t for t in (selecting, cancelling) if t], return_exceptions=True)

    async def test_direct_repeated_cancellation_cannot_interrupt_commit(self):
        ctl = self.make_controller()
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        entered, release = threading.Event(), threading.Event()
        def checkpoint(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('checkpoint barrier timed out')
        self.api.action.side_effect = checkpoint
        selecting = asyncio.create_task(ctl.select_session('ws_b'))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            for _ in range(2):
                selecting.cancel()
                await self.tick()
                self.assertFalse(selecting.done())
                self.assertTrue(ctl._action_lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await selecting
            self.assertEqual(ctl.workspace, self.candidate)
            self.assertEqual(ctl.client.channel, 'session-b')
            self.assertFalse(ctl._closed)
            self.api.action.assert_called_once_with('ws_a', 'checkpoint')
            self.assert_clean(ctl)
        finally:
            release.set()
            await asyncio.gather(selecting, return_exceptions=True)

    async def test_checkpoint_failure_warns_but_commits(self):
        ctl = self.make_controller()
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        self.api.action.side_effect = CLIError('checkpoint refused')
        self.assertEqual((await ctl.select_session('ws_b')).status, 'completed')
        self.assertEqual(ctl.workspace, self.candidate)
        self.assertEqual(self.output, ['Warning: checkpoint failed: checkpoint refused'])
        self.assert_clean(ctl)

    async def test_archived_accept_resumes_then_checkpoints_then_notifies(self):
        ctl = self.make_controller(archived=True, stopped=True)
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        self.choice = True
        self.dialog_release.set()
        generation = ctl._selection_version
        def observe(event):
            self.assertEqual(event.selection_generation, generation + 1)
            self.assertEqual(ctl.client.channel, 'session-b')
            self.assertFalse(ctl.workspace['archived'])
            self.assertEqual(ctl.workspace['agents'][0]['last_state'], 'starting')
            self.assertFalse(ctl.selection_pending)
            self.assertIsNone(ctl._selection_task)
            self.assertIsNone(ctl._selection_commit)
            self.trace.append(('selection', event.workspace_id))
        ctl.on_view_change = observe
        self.assertEqual((await ctl.select_session('ws_b')).status, 'completed')
        self.assertEqual(self.trace, [
            ('get', 'ws_b'), ('dialog', 'Unarchive it? [y/N]', False, False),
            ('unarchive', 'ws_b'), ('dialog', 'Resume 1 stopped agents? [Y/n]', True, False),
            ('resume', 'ws_b'), ('get', 'ws_b'), ('checkpoint', 'ws_a'), ('selection', 'ws_b')])
        self.assert_clean(ctl)

    async def test_duplicate_selection_is_cancelled_without_second_dialog(self):
        ctl = self.make_controller(stopped=True)
        self.assertTrue(callable(getattr(ctl, 'select_session', None)), 'select_session missing')
        selecting = asyncio.create_task(ctl.select_session('ws_b'))
        await asyncio.wait_for(self.dialog_open.wait(), 2)
        self.assertEqual((await ctl.select_session('ws_b')).status, 'cancelled')
        self.assertEqual(self.presenter.confirm.await_count, 1)
        await ctl.cancel_selection()
        self.assertEqual((await selecting).status, 'cancelled')
        self.assert_clean(ctl)

    async def test_cancel_selection_does_not_wait_for_callers_later_work(self):
        ctl = self.make_controller(stopped=True)
        continued, finish_caller = asyncio.Event(), asyncio.Event()
        outcomes = []
        async def caller():
            outcomes.append(await ctl.select_session('ws_b'))
            continued.set()
            await finish_caller.wait()
        selecting = asyncio.create_task(caller())
        cancelling = None
        try:
            await asyncio.wait_for(self.dialog_open.wait(), 2)
            cancelling = asyncio.create_task(ctl.cancel_selection())
            await asyncio.wait_for(continued.wait(), 2)
            for _ in range(4):
                await self.tick()
            self.assertTrue(cancelling.done(), 'cancellation must not own work after select_session returns')
            await cancelling
            self.assertEqual(outcomes, [ActionOutcome('cancelled', workspace_id='ws_b')])
            self.assertFalse(selecting.done())
            self.assert_clean(ctl)
        finally:
            finish_caller.set()
            await asyncio.gather(*[t for t in (selecting, cancelling) if t], return_exceptions=True)

    async def test_cancel_completed_preparation_before_commit_does_not_checkpoint(self):
        ctl = self.make_controller()
        selection = None
        async def get_candidate(ws_id, version):
            # Schedule cancellation before select_session resumes with this snapshot.
            asyncio.create_task(ctl.cancel_selection())
            return copy.deepcopy(self.candidate)
        # Own preparation remains real except this deterministic scheduling boundary.
        with patch.object(ctl, '_prepare_selection', side_effect=get_candidate):
            selection = asyncio.create_task(ctl.select_session('ws_b'))
            self.assertEqual((await selection).status, 'cancelled')
        self.api.action.assert_not_called()
        self.assertEqual(ctl.workspace, self.old)
        self.assert_clean(ctl)

    async def test_candidate_unarchive_and_refresh_snapshots_reject_revision_overlap(self):
        for phase in ('unarchive', 'refresh'):
            with self.subTest(phase=phase):
                ctl = self.make_controller(archived=phase == 'unarchive', stopped=phase == 'refresh')
                self.choice = True
                self.dialog_release.set()
                entered, release = threading.Event(), threading.Event()
                original = self.api.action.side_effect if phase == 'unarchive' else self.api.get.side_effect
                count = 0
                def blocked(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if phase == 'unarchive' or count == 2:
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError('snapshot barrier timed out')
                    return original(*args, **kwargs)
                if phase == 'unarchive':
                    self.api.action.side_effect = blocked
                else:
                    self.api.get.side_effect = blocked
                selecting = asyncio.create_task(ctl.select_session('ws_b'))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    updated = dict(self.old, name='newer same-session event')
                    ctl.on_workspace(updated)
                    release.set()
                    self.assertEqual((await selecting).status, 'cancelled')
                    self.assertEqual(ctl.workspace, updated)
                    self.assertNotIn(call('ws_a', 'checkpoint'), self.api.action.call_args_list)
                    self.assert_clean(ctl)
                finally:
                    release.set()
                    await asyncio.gather(selecting, return_exceptions=True)

    async def test_commit_revalidates_after_waiting_for_action_lock(self):
        ctl = self.make_controller()
        candidate_read = asyncio.Event()
        original = ctl._prepare_selection
        async def prepare(*args):
            candidate = await original(*args)
            candidate_read.set()
            return candidate
        await ctl._action_lock.acquire()
        selecting = None
        try:
            with patch.object(ctl, '_prepare_selection', side_effect=prepare):
                selecting = asyncio.create_task(ctl.select_session('ws_b'))
                await asyncio.wait_for(candidate_read.wait(), 2)
                await self.tick()
                self.assertIsNotNone(ctl._selection_commit)
                replacement = dict(self.old, id='ws_c', channel='session-c')
                ctl._select(replacement)
        finally:
            ctl._action_lock.release()
        self.assertEqual((await selecting).status, 'cancelled')
        self.assertEqual(ctl.workspace, replacement)
        self.api.action.assert_not_called()
        self.assert_clean(ctl)

    async def test_archive_stale_response_never_clears_later_selection(self):
        ctl = self.make_controller()
        entered, release = threading.Event(), threading.Event()
        def archive(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('archive barrier timed out')
            return copy.deepcopy(self.old)
        self.api.action.side_effect = archive
        archiving = asyncio.create_task(ctl.execute_action('archive_session', {'confirmed': True}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            ctl._select(self.candidate)
            event_count = len(self.events)
            release.set()
            self.assertEqual((await archiving).status, 'completed')
            self.assertEqual(ctl.workspace, self.candidate)
            self.assertFalse(ctl._closed)
            self.assertEqual(len(self.events), event_count)
            self.api.action.assert_called_once_with('ws_a', 'archive')
            self.assert_clean(ctl)
        finally:
            release.set()
            await asyncio.gather(archiving, return_exceptions=True)
