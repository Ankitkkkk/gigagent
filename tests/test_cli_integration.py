"""Exercise terminal clients against an isolated real server, without agent CLIs."""

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cli


class CliIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="agentchattr-cli-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.log = open(Path(cls.temp.name) / "server.log", "w+")
        cls.addClassCleanup(cls.log.close)
        sockets = [socket.socket() for _ in range(3)]
        try:
            for sock in sockets:
                sock.bind(("127.0.0.1", 0))
            ports = [sock.getsockname()[1] for sock in sockets]
        finally:
            for sock in sockets:
                sock.close()
        cls.url = f"http://127.0.0.1:{ports[0]}"
        cls.process = subprocess.Popen([
            sys.executable, "run.py", "--port", str(ports[0]),
            "--mcp-http-port", str(ports[1]), "--mcp-sse-port", str(ports[2]),
            "--data-dir", cls.temp.name + "/data", "--upload-dir", cls.temp.name + "/uploads",
        ], cwd=ROOT, stdout=cls.log, stderr=cls.log,
            env={k: v for k, v in os.environ.items() if not k.startswith("AGENTCHATTR_")})
        cls.addClassCleanup(cls.stop_server)
        for _ in range(100):
            if cls.process.poll() is not None:
                raise RuntimeError("Isolated server exited during startup")
            try:
                cli.fetch_session_token(cls.url)
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Isolated server did not start within 10 seconds")

    @classmethod
    def stop_server(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait()

    def command(self, *args, input=None):
        return subprocess.run([sys.executable, "cli.py", "--url", self.url, *args],
                              cwd=ROOT, text=True, capture_output=True, input=input, timeout=20)

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
