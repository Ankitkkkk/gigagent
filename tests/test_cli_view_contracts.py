"""Structured view notification and submission outcome contracts."""

import asyncio
import copy
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cli import ChatClient
from cli_view_contracts import ActionOutcome, SubmitOutcome, ViewEvent
from cli_workspace_chat import WorkspaceChatController


def message(message_id, text="hello", **changes):
    record = {
        "id": message_id,
        "channel": "general",
        "sender": "user",
        "text": text,
        "timestamp": message_id,
    }
    record.update(changes)
    return record


class ValueTypeTests(unittest.TestCase):
    def test_shared_contracts_are_immutable_values(self):
        event = ViewEvent("client", "messages", 1, message_ids=(7,))
        self.assertEqual(event.message_ids, (7,))
        self.assertEqual(ActionOutcome("completed").status, "completed")
        self.assertTrue(SubmitOutcome("completed").keep_running)
        with self.assertRaises(FrozenInstanceError):
            event.kind = "history"


class ClientViewEventTests(unittest.TestCase):
    def setUp(self):
        self.printed = []
        self.observed = []
        self.client = ChatClient("http://127.0.0.1:8300", output=self.printed.append)

    def bind(self):
        self.client.on_view_change = lambda event: self.observed.append(
            (event, tuple(self.client.messages), self.client.ready.is_set()))

    def test_message_event_notifies_after_cache_update_without_printing(self):
        self.bind()
        self.client.ready.set()
        self.client.handle_event({"type": "message", "data": message(1)})
        self.assertEqual(self.observed[-1][1], (1,))
        self.assertEqual(self.observed[-1][0].message_ids, (1,))
        self.assertEqual(self.observed[-1][0].kind, "messages")
        self.assertEqual(self.printed, [])

    def test_history_completion_notifies_once_after_ready_and_without_printing(self):
        self.bind()
        self.client.handle_event({"type": "history", "messages": [message(2), message(1)]})
        self.assertEqual(self.observed, [])
        self.client.handle_event({"type": "history_complete"})
        event, cached, ready = self.observed[-1]
        self.assertEqual((event.kind, event.message_ids), ("history", (1, 2)))
        self.assertEqual(cached, (2, 1))
        self.assertTrue(ready)
        self.assertEqual(self.printed, [])

    def test_update_delete_and_clear_notify_after_each_mutation(self):
        self.client.messages[1] = message(1)
        self.client.messages[2] = message(2, channel="work")
        states = []
        self.client.on_view_change = lambda event: states.append(
            (event.kind, event.message_ids, copy.deepcopy(self.client.messages)))

        self.client.handle_event({"type": "message_update", "message": message(1, "edited")})
        self.client.handle_event({"type": "delete", "ids": [1, 99]})
        self.client.handle_event({"type": "clear", "channel": "work"})

        self.assertEqual([(kind, ids) for kind, ids, _ in states], [
            ("messages", (1,)), ("messages", (1, 99)), ("messages", (2,))])
        self.assertEqual(states[0][2][1]["text"], "edited")
        self.assertNotIn(1, states[1][2])
        self.assertEqual(states[2][2], {})
        self.assertEqual(self.printed, ["History cleared: #work"])

    def test_settings_status_and_renames_emit_typed_invalidations(self):
        self.client.messages[1] = message(1, sender="claude", channel="work")
        self.bind()
        self.client.handle_event({"type": "settings", "data": {
            "channels": ["general", "work"], "username": "Pat"}})
        self.client.handle_event({"type": "agents", "data": ["claude"]})
        self.client.handle_event({"type": "status", "data": {"paused": False}})
        self.client.handle_event({"type": "agent_renamed", "old_name": "claude",
                                  "new_name": "reviewer"})
        self.client.handle_event({"type": "channel_renamed", "old_name": "work",
                                  "new_name": "review"})

        events = [item[0] for item in self.observed]
        self.assertEqual([event.kind for event in events],
                         ["settings", "status", "status", "messages", "channel"])
        self.assertEqual(events[-2].message_ids, (1,))
        self.assertEqual(events[-1].message_ids, (1,))
        self.assertEqual(self.client.messages[1]["sender"], "reviewer")
        self.assertEqual(self.client.messages[1]["channel"], "review")
        self.assertEqual([event.revision for event in events], [1, 2, 3, 4, 5])

    def test_show_message_and_history_keep_legacy_output_without_hook(self):
        self.client.messages[1] = message(1)
        self.client.history()
        self.client.show_message(message(2, attachments=[{"name": "image"}],
                                         metadata={"choices": ["yes", "no"]}))
        self.assertEqual(self.printed, [
            "# general", "[] user: hello", "[] user: hello",
            "  Attachment: image", "  Choices: yes | no"])


class _WaitingSocket:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Future()


class ConnectionViewEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_receiver_reports_connecting_connected_and_reconnecting(self):
        client = ChatClient("http://127.0.0.1:8300", output=lambda text: None)
        observed = []
        connected = asyncio.Event()

        def notify(event):
            observed.append((event.kind, client.connection_state, event.revision))
            if client.connection_state == "connected":
                connected.set()

        client.on_view_change = notify
        with patch("cli.fetch_session_token", return_value="token"), \
             patch("websockets.asyncio.client.connect", return_value=_WaitingSocket()):
            receiver = asyncio.create_task(client.receive_forever())
            await asyncio.wait_for(connected.wait(), 1)
            receiver.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await receiver

        self.assertEqual([state for _, state, _ in observed],
                         ["connecting", "connected", "reconnecting"])
        self.assertEqual([kind for kind, _, _ in observed], ["connection"] * 3)
        self.assertEqual(client.connection_state, "reconnecting")


class SubmissionOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.output = []
        self.client = ChatClient("http://127.0.0.1:8300", username="Pat",
                                 output=self.output.append)
        self.client.channels = ["general", "work"]
        self.client.send = AsyncMock(return_value=True)

    async def test_message_result_exposes_transport_acceptance(self):
        outcome = await self.client.submit_outcome("hello")
        self.assertEqual(outcome, SubmitOutcome("completed", sent=True))
        self.client.send.assert_awaited_once_with({
            "type": "message", "text": "hello", "channel": "general", "sender": "Pat"})

    async def test_failed_send_retains_structured_failure_and_legacy_chat_lifetime(self):
        self.client.send.return_value = False
        outcome = await self.client.submit_outcome("hello")
        self.assertEqual(outcome, SubmitOutcome("failed", sent=False))
        self.assertTrue(await self.client.submit("hello"))

    async def test_invalid_join_and_create_fail_but_legacy_submit_stays_open(self):
        join = await self.client.submit_outcome("/join missing")
        create = await self.client.submit_outcome("/create Bad_Name")
        self.assertEqual(join.status, "failed")
        self.assertEqual(create.status, "failed")
        self.assertTrue(await self.client.submit("/join missing"))
        self.assertTrue(await self.client.submit("/create Bad_Name"))
        self.client.send.assert_not_awaited()

    async def test_quit_and_empty_input_preserve_legacy_meaning(self):
        self.assertEqual(await self.client.submit_outcome(" /quit "),
                         SubmitOutcome("completed", keep_running=False))
        self.assertFalse(await self.client.submit("/exit"))
        self.assertEqual(await self.client.submit_outcome("  "), SubmitOutcome("completed"))

    async def test_channel_create_reports_request_failure_and_clears_pending(self):
        self.client.send.return_value = False
        outcome = await self.client.submit_outcome("/create new-work")
        self.assertEqual(outcome.status, "failed")
        self.assertFalse(outcome.sent)
        self.assertIsNone(self.client.pending_channel)

    async def test_join_existing_channel_emits_one_view_event(self):
        observed = []
        self.client.on_view_change = observed.append
        outcome = await self.client.submit_outcome("/join work")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(self.client.channel, "work")
        self.assertEqual([event.kind for event in observed], ["history"])

    async def test_create_existing_channel_emits_one_view_event(self):
        observed = []
        self.client.on_view_change = observed.append
        outcome = await self.client.submit_outcome("/create work")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(self.client.channel, "work")
        self.assertEqual([event.kind for event in observed], ["history"])

    async def test_settings_resolving_pending_channel_emits_one_view_event(self):
        observed = []
        self.client.pending_channel = "work"
        self.client.on_view_change = observed.append
        self.client.handle_event({"type": "settings", "data": {
            "channels": ["general", "work"], "username": "Pat"}})
        self.assertEqual(self.client.channel, "work")
        self.assertIsNone(self.client.pending_channel)
        self.assertEqual([event.kind for event in observed], ["settings"])


class ControllerViewEventTests(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.client = ChatClient("http://127.0.0.1:8300", output=self.output.append)
        self.api = Mock()
        self.agent = {
            "agent_id": "ag_a", "registry_name": "claude-1", "provider": "claude",
            "cwd": None, "last_state": "exited", "native_session_id": None,
            "unread_count": 0, "history_mode": "literal", "history_state": "done",
        }
        self.workspace = {"id": "ws_a", "channel": "ws-a", "name": "billing",
                          "agents": [copy.deepcopy(self.agent)]}
        self.controller = WorkspaceChatController(self.client, self.api)

    def test_bind_view_installs_callbacks_and_selection_notifies_after_version(self):
        presentation = Mock()
        before_generation = self.controller._selection_version
        observed = []

        def notify(event):
            self.assertEqual(self.controller._selection_version, before_generation + 1)
            self.assertEqual(event.selection_generation, before_generation + 1)
            observed.append((event, self.controller.workspace))

        self.controller.bind_view(presentation, notify)
        self.assertIs(self.controller.presentation, presentation)
        self.assertEqual(self.client.on_workspace, self.controller.on_workspace)
        self.assertEqual(self.client.on_settings, self.controller.on_settings)

        self.controller._select(copy.deepcopy(self.workspace))

        event, selected = observed[-1]
        self.assertEqual(event.kind, "selection")
        self.assertEqual(event.workspace_id, "ws_a")
        self.assertEqual(selected["id"], "ws_a")

    def test_workspace_update_notifies_after_revision_without_status_printing(self):
        self.controller.workspace = copy.deepcopy(self.workspace)
        self.controller.workspace["agents"][0]["last_state"] = "starting"
        self.controller._agent_states = {
            "ag_a": self.controller._agent_status(self.controller.workspace["agents"][0])}
        before_revision = self.controller._state_revision
        observed = []

        def notify(event):
            self.assertEqual(self.controller._state_revision, before_revision + 1)
            self.assertEqual(event.revision, before_revision + 1)
            current = self.controller.workspace["agents"][0]
            self.assertEqual(self.controller._agent_states["ag_a"],
                             self.controller._agent_status(current))
            self.assertIn("ag_a", self.controller._failed_launches)
            observed.append((event, current["last_state"]))

        self.controller.bind_view(Mock(), notify)
        updated = copy.deepcopy(self.workspace)
        updated["agents"][0]["last_state"] = "exited"
        updated["agents"][0]["last_error"] = "launch failed"

        self.controller.on_workspace(updated)

        event, state = observed[-1]
        self.assertEqual(event.kind, "agent_state")
        self.assertEqual(event.workspace_id, "ws_a")
        self.assertEqual(event.selection_generation, self.controller._selection_version)
        self.assertEqual(state, "exited")
        self.assertEqual(self.output, [])

    def test_controller_refusal_still_flows_through_output_in_view_mode(self):
        observed = []
        self.controller.workspace = copy.deepcopy(self.workspace)
        self.controller.bind_view(Mock(), observed.append)
        result = asyncio.run(self.controller.handle("/join general"))
        self.assertEqual(result, "continue")
        self.assertEqual(self.output, [
            "Use /sessions to switch sessions, or plain --channel mode to join/create channels."])
        self.assertEqual(observed, [])


if __name__ == "__main__":
    unittest.main()
