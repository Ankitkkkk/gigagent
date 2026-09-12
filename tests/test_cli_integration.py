"""Exercise terminal clients against an isolated real server, without agent CLIs."""

import asyncio
import json
import unittest
from unittest.mock import patch

from _cli_server import IsolatedCliServer, cli


class CliIntegrationTests(IsolatedCliServer):
    def test_send_acknowledgment_read_and_status(self):
        result = self.command("send", "--name", "TerminalTester", "--json", "@claude inspect terminal test")
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.loads(result.stdout)
        self.assertEqual(sent["sender"], "TerminalTester")
        result = self.command("read", "--json", "--limit", "100")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(sent, json.loads(result.stdout))
        result = self.command("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("paused", json.loads(result.stdout))

    def test_stdin_send_and_invalid_channel_exit_code(self):
        result = self.command("send", "--json", "-", input="piped\nmessage")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["text"], "piped\nmessage")
        result = self.command("read", "--channel", "does-not-exist", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_live_chat_channel_creation_and_command_acknowledgment(self):
        async def scenario():
            output = []
            client = cli.ChatClient(self.url, output=output.append)
            task = asyncio.create_task(client.receive_forever())
            try:
                async with asyncio.timeout(10):
                    await client.ready.wait()
                    await client.submit("/create terminal-work")
                    while client.channel != "terminal-work":
                        await asyncio.sleep(0.02)
                    await client.submit("live terminal message")
                    while not any("live terminal message" in line for line in output):
                        await asyncio.sleep(0.02)
                await client.submit("/join general")
                self.assertEqual(client.channel, "general")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(scenario())
        result = self.command("channels", "--json")
        self.assertIn("terminal-work", json.loads(result.stdout))
        result = self.command("send", "--channel", "terminal-work", "--json", "/continue")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["channel"], "terminal-work")

    def test_interactive_prompt_send_and_quit(self):
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.input import create_pipe_input
            from prompt_toolkit.output import DummyOutput
        except ImportError:
            self.skipTest("Install requirements-cli.txt for interactive prompt coverage")

        async def scenario():
            output = []
            client = cli.ChatClient(self.url, output=output.append)
            with create_pipe_input() as pipe:
                def session(**kwargs):
                    from prompt_toolkit.completion import CompleteEvent
                    from prompt_toolkit.document import Document
                    client.agent_names = ["claude-2"]
                    completions = list(kwargs["completer"].get_completions(
                        Document("@claude-"), CompleteEvent()))
                    self.assertEqual([item.text for item in completions], ["@claude-2"])
                    return PromptSession(input=pipe, output=DummyOutput(), **kwargs)
                with patch("prompt_toolkit.PromptSession", side_effect=session):
                    task = asyncio.create_task(cli.interactive(client))
                    try:
                        async with asyncio.timeout(10):
                            await client.ready.wait()
                            pipe.send_text("prompt integration message\n")
                            while not any("prompt integration message" in line for line in output):
                                await asyncio.sleep(0.02)
                            pipe.send_text("/quit\n")
                            await task
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            self.assertIsNone(client.websocket)
        asyncio.run(scenario())

    def test_live_client_reconnects_after_connection_closes(self):
        async def scenario():
            output = []
            client = cli.ChatClient(self.url, output=output.append)
            task = asyncio.create_task(client.receive_forever())
            try:
                async with asyncio.timeout(10):
                    await client.ready.wait()
                    first = client.websocket
                    await first.close()
                    while client.websocket is first or not client.ready.is_set():
                        await asyncio.sleep(0.02)
                    self.assertEqual(sum(line.startswith("Connected to") for line in output), 2)
                    await client.submit("reconnected terminal message")
                    while not any("reconnected terminal message" in line for line in output):
                        await asyncio.sleep(0.02)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
