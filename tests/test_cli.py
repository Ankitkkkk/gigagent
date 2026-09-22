import asyncio
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli import ChatClient, SessionTokenParser, build_parser, local_url, terminal_text


def message(mid, text, channel="general"):
    return {"id": mid, "text": text, "sender": "claude", "channel": channel,
            "timestamp": mid, "time": "12:00"}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.client = ChatClient("http://127.0.0.1:8300", output=self.output.append)
        self.client.handle_event({"type": "settings", "data": {
            "username": "Pat", "channels": ["general", "work"]}})

    def test_history_boundary_handles_live_messages_and_deduplicates(self):
        self.client.handle_event({"type": "history", "messages": [message(0, "saved")]})
        self.client.handle_event({"type": "message", "data": message(1, "live")})
        self.client.handle_event({"type": "status", "data": {"paused": False}})
        self.assertEqual(self.output, [])
        self.assertFalse(self.client.ready.is_set())
        self.client.handle_event({"type": "history_complete"})
        self.client.handle_event({"type": "message", "data": message(1, "live")})
        self.assertEqual(sum("live" in line for line in self.output), 1)
        self.assertTrue(self.client.ready.is_set())

    def test_channel_switch_shows_only_selected_channel(self):
        self.client.remember(message(0, "general text"))
        self.client.remember(message(1, "work text", "work"))
        asyncio.run(self.client.submit("/join #work"))
        self.assertEqual(self.client.channel, "work")
        self.assertIn("work text", "\n".join(self.output))
        self.assertNotIn("general text", "\n".join(self.output))

    def test_delete_clear_and_rename_update_cached_history(self):
        for mid, ch in [(0, "general"), (1, "work"), (2, "work")]:
            self.client.remember(message(mid, "text", ch))
        self.client.handle_event({"type": "delete", "ids": [2]})
        self.client.handle_event({"type": "clear", "channel": "general"})
        self.client.handle_event({"type": "agent_renamed", "old_name": "claude", "new_name": "reviewer"})
        self.assertEqual(list(self.client.messages), [1])
        self.assertEqual(self.client.messages[1]["sender"], "reviewer")

    def test_bootstrap_parses_only_script_token(self):
        parser = SessionTokenParser()
        parser.feed('<p>window.__SESSION_TOKEN__="wrong";</p><script src="x.js"></script>')
        parser.feed('<script>window.__SESSION_TOKEN__="test-token";</script>')
        self.assertEqual(parser.token, "test-token")

    def test_local_urls_only_and_terminal_controls_removed(self):
        for url in ["https://example.com", "http://localhost.evil", "http://user@localhost",
                    "http://127.0.0.1:8300/path", "http://localhost?token=secret"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                local_url(url)
        self.assertEqual(local_url("http://[::1]:8300/"), "http://[::1]:8300")
        self.assertEqual(terminal_text("hello\x1b\x07\r\x9b\nworld"), "hello\nworld")

    def test_shell_options_work_before_or_after_subcommand(self):
        for argv in [["--channel", "work", "--json", "read"],
                     ["read", "--channel", "work", "--json"]]:
            args = build_parser().parse_args(argv)
            self.assertEqual(args.channel, "work")
            self.assertTrue(args.json)
            self.assertEqual(args.history, 30)

    def test_no_resume_accepts_bare_and_explicit_chat_only(self):
        for argv in [["--session", "billing", "--no-resume"],
                     ["chat", "--session", "billing", "--no-resume"]]:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                try:
                    args = build_parser().parse_args(argv)
                except SystemExit as error:
                    self.fail(f"Interactive chat rejected --no-resume (exit {error.code})")
                self.assertTrue(args.no_resume)
                self.assertEqual(args.session, "billing")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["sessions", "--no-resume"])
        self.assertEqual(caught.exception.code, 2)

    def test_readme_cli_examples_parse_offline(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        examples = [line for line in readme.splitlines()
                    if line.startswith(("python cli.py", "python gigagent.py"))]
        self.assertIn("python gigagent.py --session billing --no-resume", examples)
        for line in examples:
            with self.subTest(example=line), redirect_stderr(io.StringIO()):
                try:
                    build_parser().parse_args(shlex.split(line)[2:])
                except SystemExit as error:
                    self.fail(f"README example rejected (exit {error.code}): {line}")


class CliSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.output = []
        self.client = ChatClient("http://localhost:8300", username="Pat", output=self.output.append)
        self.client.websocket = AsyncMock()

    async def test_human_send_preserves_mentions_and_channel(self):
        self.client.channel = "work"
        self.client.ready.set()
        await self.client.submit("@claude-2 review this")
        event = json.loads(self.client.websocket.send.call_args.args[0])
        self.assertEqual(event, {"type": "message", "sender": "Pat", "channel": "work",
                                 "text": "@claude-2 review this"})

    async def test_disconnected_message_is_not_queued_or_sent(self):
        await self.client.submit("do something")
        self.client.websocket.send.assert_not_called()
        self.assertIn("not sent", self.output[-1])

    async def test_create_waits_for_server_settings_before_switching(self):
        self.client.ready.set()
        await self.client.submit("/create new-work")
        self.assertEqual(self.client.channel, "general")
        self.client.handle_event({"type": "settings", "data": {
            "channels": ["general", "new-work"]}})
        self.assertEqual(self.client.channel, "new-work")
        self.assertIsNone(self.client.pending_channel)

    async def test_invalid_channel_does_not_send(self):
        self.client.ready.set()
        for name in ["bad_name", "a" * 21, "-bad", ""]:
            await self.client.submit("/create " + name)
        self.client.websocket.send.assert_not_called()

    async def test_quit_is_local(self):
        self.assertFalse(await self.client.submit("/quit"))
        self.client.websocket.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
