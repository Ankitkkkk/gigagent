"""Presentation-state, sanitation, and legacy-output regressions."""

import asyncio
import builtins
from collections import OrderedDict
import io
from contextlib import redirect_stdout
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import cli
from cli import ChatClient
from cli_api import CLIError
import cli_view_contracts as contracts
import cli_workspace_chat as chat
import cli_workspaces
from cli_tui_state import (DraftStore, NoticeStore, TuiState, Viewport,
                           body_text, clip_cells, label_text, layout_mode)


class SanitationTests(unittest.TestCase):
    def test_contract_module_imports_without_cli_or_prompt_toolkit(self):
        module_name = '_isolated_cli_view_contracts'
        spec = importlib.util.spec_from_file_location(
            module_name, Path(contracts.__file__))
        module = importlib.util.module_from_spec(spec)
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in {'cli', 'cli_workspaces', 'cli_workspace_chat'} or \
                    name.startswith('prompt_toolkit'):
                raise AssertionError(f'forbidden dependency: {name}')
            return original_import(name, *args, **kwargs)

        sys.modules[module_name] = module
        self.addCleanup(sys.modules.pop, module_name, None)
        with patch('builtins.__import__', side_effect=guarded_import):
            spec.loader.exec_module(module)
        self.assertEqual(module.terminal_text('safe\x1b'), 'safe')

    def test_canonical_sanitisers_are_identity_preserving_reexports(self):
        self.assertIs(cli.terminal_text, contracts.terminal_text)
        self.assertIs(cli_workspaces._safe, contracts._safe)
        self.assertIs(chat._safe, contracts._safe)
        self.assertEqual(contracts.terminal_text("hello\x1b\x07\r\x9b\n\tworld"),
                         "hello\n\tworld")
        self.assertEqual(contracts._safe("hello\x1b\x07\r\x9b\n\tworld"),
                         "helloworld")

    def test_labels_remove_format_and_terminal_controls(self):
        value = "left\u202eright\u200b\x1b[2J"
        self.assertEqual(label_text(value), "leftright[2J")

    def test_bodies_keep_newlines_and_expand_tabs(self):
        self.assertEqual(body_text("row\tvalue\nnext\u202eline\x1b"),
                         "row value\nnextline")

    def test_clip_cells_obeys_unicode_width_and_ellipsis(self):
        self.assertEqual(clip_cells("A界BC", 4), "A界…")
        self.assertEqual(clip_cells("A界BC", 3), "A…")
        self.assertEqual(clip_cells("A界BC", 1), "…")
        self.assertEqual(clip_cells("A界BC", 0), "")
        self.assertEqual(clip_cells("界", 2), "界")

    def test_clip_cells_never_splits_combining_cluster(self):
        self.assertEqual(clip_cells("e\u0301xy", 2), "e\u0301…")
        self.assertEqual(clip_cells("e\u0301xy", 1), "…")


class TranscriptOrderTests(unittest.TestCase):
    @staticmethod
    def records():
        return OrderedDict([
            (30, {'id': 30, 'timestamp': 3, 'channel': 'general',
                  'sender': 'bot', 'text': 'third', 'time': '03:00'}),
            (10, {'id': 10, 'timestamp': 1, 'channel': 'general',
                  'sender': 'bot', 'text': 'first', 'time': '01:00'}),
            (20, {'id': 20, 'timestamp': 2, 'channel': 'other',
                  'sender': 'bot', 'text': 'hidden', 'time': '02:00'}),
            (5, {'id': 5, 'timestamp': 2, 'sender': 'bot',
                 'text': 'second', 'time': '02:00'}),
        ])

    def test_shared_transcript_filters_and_sorts_nonmonotonic_records(self):
        rows = contracts.channel_transcript(self.records(), 'general')
        self.assertEqual([row['id'] for row in rows], [10, 5, 30])

    def test_client_history_uses_shared_order_before_existing_limit(self):
        output = []
        client = ChatClient('http://127.0.0.1:8300', history_limit=2,
                            output=output.append)
        client.messages = self.records()
        client.history()
        self.assertEqual(output, [
            '# general', '[02:00] bot: second', '[03:00] bot: third'])


class DraftStoreTests(unittest.TestCase):
    def test_drafts_refuse_new_destination_without_eviction(self):
        drafts = DraftStore()
        for n in range(50):
            self.assertTrue(drafts.set(('session', str(n)), 'unsent'))
        self.assertFalse(drafts.can_open(('session', 'new')))
        self.assertTrue(drafts.can_open(('session', '0')))
        self.assertEqual(drafts.get(('session', '0')), 'unsent')
        self.assertEqual(drafts.capacity_notice, '50 unsent drafts; send or clear one')
        self.assertEqual(len(drafts), 50)
        self.assertTrue(drafts.set(('session', '0'), 'edited'))
        self.assertEqual(drafts.get(('session', '0')), 'edited')
        self.assertEqual(len(drafts), 50)

    def test_utf8_limit_accepts_exact_boundary_and_preserves_surrogates(self):
        drafts = DraftStore()
        key = ('channel', 'general')
        boundary = 'a' * 65533 + '\udc80'
        oversize = boundary + 'b'
        self.assertEqual(len(boundary.encode('utf-8', 'surrogatepass')), 65536)
        self.assertEqual(len(oversize.encode('utf-8', 'surrogatepass')), 65537)
        self.assertTrue(drafts.set(key, boundary, cursor=len(boundary)))
        self.assertEqual(drafts.get(key), boundary)
        self.assertEqual(
            drafts.get(key).encode('utf-8', 'surrogatepass').decode(
                'utf-8', 'surrogatepass'), boundary)
        self.assertFalse(drafts.set(key, oversize, cursor=1))
        self.assertEqual(drafts.get(key), boundary)
        self.assertEqual(drafts.get_cursor(key), len(boundary))

    def test_clearing_at_capacity_frees_space_for_new_destinations(self):
        drafts = DraftStore()
        for n in range(50):
            drafts.set(('session', str(n)), 'unsent')
        drafts.set(('session', '0'), '')
        self.assertTrue(drafts.can_open(('session', 'new')))
        self.assertTrue(drafts.set(('session', 'new'), 'draft'))
        self.assertEqual(len(drafts), 50)
        drafts.clear(('session', '1'))
        self.assertTrue(drafts.can_open(('session', 'another')))
        self.assertTrue(drafts.set(('session', 'another'), 'draft'))
        self.assertEqual(len(drafts), 50)

    def test_empty_text_removes_entry_and_cursor(self):
        drafts = DraftStore()
        key = ('session', 'ws_one')
        drafts.set(key, 'draft', cursor=3)
        self.assertTrue(drafts.set(key, '', cursor=1))
        self.assertEqual(drafts.get(key), '')
        self.assertEqual(drafts.get_cursor(key), 0)
        self.assertEqual(len(drafts), 0)

    def test_cursor_updates_clamp_without_creating_draft(self):
        drafts = DraftStore()
        key = ('session', 'ws_one')
        drafts.set_cursor(key, 12)
        self.assertEqual(drafts.get_cursor(key), 0)
        self.assertEqual(len(drafts), 0)
        drafts.set(key, 'draft', cursor=99)
        self.assertEqual(drafts.get_cursor(key), 5)
        drafts.set_cursor(key, -4)
        self.assertEqual(drafts.get_cursor(key), 0)
        drafts.set_cursor(key, 2)
        drafts.set(key, 'x')
        self.assertEqual(drafts.get_cursor(key), 1)

    def test_mandatory_navigation_does_not_bypass_composing_limits(self):
        drafts = DraftStore()
        for n in range(50):
            drafts.set(('session', str(n)), 'unsent')
        key = ('session', 'postarchive')
        self.assertTrue(drafts.can_open(key, mandatory=True))
        self.assertFalse(drafts.set(key, 'still composing'))
        self.assertEqual(drafts.get(key), '')
        self.assertEqual(len(drafts), 50)

    def test_items_keep_insertion_order_and_expose_only_text(self):
        drafts = DraftStore()
        drafts.set(('session', 'b'), 'second', cursor=2)
        drafts.set(('session', 'a'), 'first', cursor=1)
        drafts.set(('session', 'b'), 'edited')
        self.assertEqual(tuple(drafts.items()), (
            (('session', 'b'), 'edited'), (('session', 'a'), 'first')))


class NoticeStoreTests(unittest.TestCase):
    def test_empty_add_and_terminal_newline_do_not_store_blank_lines(self):
        notices = NoticeStore()
        notices.add('')
        notices.add('first\n')
        notices.add('second\n\nthird\n')
        self.assertEqual(notices.lines, ('first', 'second', '', 'third'))
        self.assertEqual(notices.omitted, 0)

    def test_notices_split_sanitize_and_never_write_stdout(self):
        notices = NoticeStore()
        output = io.StringIO()
        with redirect_stdout(output):
            notices.add('row\tvalue\nnext\u202eline\x1b')
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(notices.lines, ('row value', 'nextline'))
        self.assertEqual(len(notices), 2)
        self.assertEqual(notices.omitted, 0)

    def test_notices_keep_newest_thousand_and_count_omissions(self):
        notices = NoticeStore()
        notices.add('\n'.join(f'line {n}' for n in range(1001)))
        self.assertEqual(len(notices), 1000)
        self.assertEqual(notices.lines[0], 'line 1')
        self.assertEqual(notices.lines[-1], 'line 1000')
        self.assertEqual(notices.omitted, 1)


class LayoutTests(unittest.TestCase):
    def test_layout_breakpoints(self):
        cases = [
            ((79, 40), 'small'), ((120, 17), 'small'), ((80, 18), 'compact'),
            ((109, 30), 'compact'), ((120, 23), 'compact'), ((110, 24), 'wide'),
        ]
        for dimensions, expected in cases:
            with self.subTest(dimensions=dimensions):
                self.assertEqual(layout_mode(*dimensions), expected)


class ViewportTests(unittest.TestCase):
    @staticmethod
    def messages(*ids):
        return OrderedDict((message_id, {'id': message_id}) for message_id in ids)

    def test_initial_sync_and_follow_do_not_count_history(self):
        viewport = Viewport()
        messages = self.messages(1, 2, 3)
        viewport.sync(messages, changed_ids=(1, 2, 3))
        self.assertTrue(viewport.follow)
        self.assertEqual(viewport.anchor_id, 3)
        self.assertEqual(viewport.new_ids, set())

    def test_following_stays_clear_after_initial_sync(self):
        viewport = Viewport()
        viewport.sync(self.messages(1), changed_ids=(1,))
        viewport.sync(self.messages(1, 2), changed_ids=(2,))
        self.assertTrue(viewport.follow)
        self.assertEqual(viewport.anchor_id, 2)
        self.assertEqual(viewport.new_ids, set())

    def test_unfollowed_view_counts_only_new_changed_ids(self):
        viewport = Viewport()
        viewport.sync(self.messages(1, 2, 3))
        viewport.anchor(2, self.messages(1, 2, 3))
        viewport.sync(self.messages(1, 2, 3, 4), changed_ids=(2, 4))
        self.assertFalse(viewport.follow)
        self.assertEqual(viewport.anchor_id, 2)
        self.assertEqual(viewport.new_ids, {4})
        viewport.mark_seen()
        self.assertTrue(viewport.follow)
        self.assertEqual(viewport.anchor_id, 4)
        self.assertEqual(viewport.new_ids, set())

    def test_reconnect_suppresses_false_new_ids(self):
        viewport = Viewport()
        viewport.sync(self.messages(1, 2))
        viewport.anchor(1, self.messages(1, 2))
        viewport.sync(self.messages(1, 2, 3), changed_ids=(3,), reconnect=True)
        self.assertEqual(viewport.new_ids, set())

    def test_deleted_anchor_chooses_next_then_nearest_previous(self):
        viewport = Viewport()
        original = self.messages(1, 2, 3, 4)
        viewport.sync(original)
        viewport.anchor(2, original)
        viewport.sync(self.messages(1, 3, 4), deleted_ids=(2,))
        self.assertEqual(viewport.anchor_id, 3)
        viewport.anchor(4, self.messages(1, 3, 4))
        viewport.sync(self.messages(1, 3), deleted_ids=(4,))
        self.assertEqual(viewport.anchor_id, 3)

    def test_deleted_anchor_without_survivor_resumes_following(self):
        viewport = Viewport()
        viewport.sync(self.messages(1))
        viewport.anchor(1, self.messages(1))
        viewport.sync(self.messages(1, 2), changed_ids=(2,))
        self.assertEqual(viewport.new_ids, {2})
        viewport.sync(self.messages(3), changed_ids=(3,), deleted_ids=(1, 2))
        self.assertTrue(viewport.follow)
        self.assertEqual(viewport.anchor_id, 3)
        self.assertEqual(viewport.new_ids, set())

    def test_anchor_ignores_unknown_message_id(self):
        viewport = Viewport()
        messages = self.messages(1, 2)
        viewport.sync(messages)
        viewport.anchor(1, messages)
        viewport.new_ids.add(2)
        viewport.anchor(99, messages)
        self.assertFalse(viewport.follow)
        self.assertEqual(viewport.anchor_id, 1)
        self.assertEqual(viewport.new_ids, {2})

    def test_anchor_recovery_uses_supplied_nonmonotonic_order(self):
        viewport = Viewport()
        rows = contracts.channel_transcript(TranscriptOrderTests.records(), 'general')
        original = OrderedDict((row['id'], row) for row in rows)
        viewport.sync(original)
        viewport.anchor(5, original)
        del original[5]
        viewport.sync(original, deleted_ids=(5,))
        self.assertEqual(viewport.anchor_id, 30)

    def test_updates_do_not_count_and_deleted_new_ids_are_pruned(self):
        viewport = Viewport()
        viewport.sync(self.messages(1, 2))
        viewport.anchor(1, self.messages(1, 2))
        viewport.sync(self.messages(1, 2), changed_ids=(2,))
        self.assertEqual(viewport.new_ids, set())
        viewport.sync(self.messages(1, 2, 3), changed_ids=(3,))
        self.assertEqual(viewport.new_ids, {3})
        viewport.sync(self.messages(1, 2), deleted_ids=(3,))
        self.assertEqual(viewport.new_ids, set())


class TuiStateTests(unittest.TestCase):
    def test_defaults_are_per_instance_and_presentation_only(self):
        first = TuiState()
        second = TuiState()
        first.drafts.set(('channel', 'general'), 'draft')
        first.notices.add('notice')
        first.viewport.new_ids.add(7)
        self.assertEqual(second.drafts.get(('channel', 'general')), '')
        self.assertEqual(second.notices.lines, ())
        self.assertEqual(second.viewport.new_ids, set())
        self.assertIsNone(first.selected_session_id)
        self.assertIsNone(first.selected_agent_id)
        self.assertEqual(first.search, '')
        self.assertEqual(first.focus_name, 'composer')
        self.assertFalse(hasattr(first, 'messages'))
        self.assertFalse(hasattr(first, 'workspace'))


class ResumeSanitationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def workspace():
        return {'id': 'ws_one', 'agents': [{
            'agent_id': 'ag_one', 'registry_name': 'codex-1',
            'last_state': 'exited', 'cwd': '/',
        }]}

    async def test_tui_resume_refusal_retains_lines_and_expands_tabs(self):
        received = []

        class Presentation:
            def notice(self, text):
                received.append(text)

        client = ChatClient('http://127.0.0.1:8300')
        controller = chat.WorkspaceChatController(client, object())
        controller.bind_view(Presentation(), lambda event: None)

        async def confirm(*args, **kwargs):
            return True

        async def mutate(*args, **kwargs):
            raise CLIError('first line\nnext\tfield\x1b', 400)

        api = type('API', (), {'action': lambda *args, **kwargs: None})()
        await chat._resume_workspace(
            api, self.workspace(), confirm=confirm, notice=controller._notice,
            mutate=mutate, still_current=lambda: True, no_resume=False)
        self.assertEqual(received, ['first line\nnext    field'])

    async def test_legacy_resume_adapter_preserves_historical_bytes(self):
        output = []

        class API:
            def action(self, *args, **kwargs):
                raise CLIError('first line\nnext\tfield\x1b', 400)

        async def prompt(*args, **kwargs):
            return 'y'

        await chat._offer_resume(API(), self.workspace(), prompt, output.append, False)
        self.assertEqual(output[-1], 'first linenextfield')

    async def test_failed_action_uses_body_policy_before_legacy_or_tui_sink(self):
        output = []
        client = ChatClient('http://127.0.0.1:8300', output=output.append)
        controller = chat.WorkspaceChatController(client, object())
        outcome = controller._failed_action(CLIError('first line\nnext\tfield\x1b', 400))
        self.assertEqual(outcome.message, 'first line\nnext\tfield')
        self.assertEqual(output, ['first line\nnext\tfield'])


class DraftEntriesTests(unittest.TestCase):
    def test_entries_include_cursor(self):
        from cli_tui_state import DraftStore
        drafts = DraftStore()
        drafts.set(('session', 'a'), 'hello', cursor=2)
        drafts.set(('channel', 'general'), 'hi')
        self.assertEqual(list(drafts.entries()),
                         [(('session', 'a'), 'hello', 2), (('channel', 'general'), 'hi', 2)])


if __name__ == '__main__':
    unittest.main()
