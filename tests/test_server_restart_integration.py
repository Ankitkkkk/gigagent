"""Real run.py in-place restart with persistent chat and inert agent terminal."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

try:
    from _cli_server import (ROOT, isolated_environment, log_excerpt, stop_process,
                             temporary_ports)
except ModuleNotFoundError:
    from tests._cli_server import (ROOT, isolated_environment, log_excerpt, stop_process,
                                   temporary_ports)

import cli
from cli_workspace_chat import WorkspaceChatController
from cli_workspaces import WorkspaceAPI
from tests._tui_harness import application_harness


@unittest.skipUnless(shutil.which("tmux") and sys.platform.startswith("linux"),
                     "Requires Linux /proc and tmux")
class ServerRestartIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="agentchattr-restart-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.log_path = root / "server.log"
        cls.log = cls.log_path.open("w+")
        cls.addClassCleanup(cls.log.close)
        cls.ports = temporary_ports()
        cls.url = f"http://127.0.0.1:{cls.ports[0]}"
        cls.data_dir = root / "data"
        cls.upload_dir = root / "uploads"
        cls.workdir = root / "work with spaces"
        cls.workdir.mkdir()
        shim_dir = root / "bin"
        shim_dir.mkdir()
        shim_text = (f"#!{sys.executable}\nimport sys\n"
                     "print('INERT PROVIDER READY', flush=True)\n"
                     "for line in sys.stdin:\n"
                     "    print('INERT PROVIDER RECEIVED', flush=True)\n")
        for provider in ("claude", "codex", "gemini", "agy", "kimi", "qwen", "kilo",
                         "codebuddy", "copilot"):
            shim = shim_dir / provider
            shim.write_text(shim_text, encoding="utf-8")
            shim.chmod(0o755)
        cls.env = isolated_environment(root, {
            "PATH": str(shim_dir) + os.pathsep + os.defpath,
        })
        cls.process = None

        def stop_child():
            if cls.process is not None:
                stop_process(cls.process)

        cls.addClassCleanup(subprocess.run, ["tmux", "kill-server"], env=cls.env,
                            capture_output=True, timeout=5)
        cls.addClassCleanup(stop_child)
        cls.argv = [
            sys.executable, "-u", "run.py", "--port", str(cls.ports[0]),
            "--mcp-http-port", str(cls.ports[1]), "--mcp-sse-port", str(cls.ports[2]),
            "--data-dir", str(cls.data_dir), "--upload-dir", str(cls.upload_dir),
        ]
        cls.process = subprocess.Popen(cls.argv, cwd=ROOT, stdout=cls.log, stderr=cls.log,
                                       env=cls.env)
        cls._wait_for_boot()
        cls.api = WorkspaceAPI(cls.url)

    @classmethod
    def _wait_for_boot(cls, old_instance=None, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                break
            try:
                token = cli.fetch_session_token(cls.url)
                status = cls._request(token, "GET", "/api/server")
                if (status["state"] == "ready" and
                        (old_instance is None or status["instance_id"] != old_instance)):
                    return token, status
            except (OSError, ValueError, KeyError, HTTPError, URLError):
                pass
            time.sleep(.05)
        raise RuntimeError("Isolated server failed readiness:\n" + log_excerpt(cls.log_path))

    @classmethod
    def _request(cls, token, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(cls.url + path, data=data, method=method,
                          headers={"X-Session-Token": token,
                                   **({"Content-Type": "application/json"} if data else {})})
        with build_opener().open(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def tearDown(self):
        result = self._outcome.result
        if any(test is self or getattr(test, "test_case", None) is self
               for test, _ in result.failures + result.errors):
            sys.stderr.write("\nIsolated server log:\n" + log_excerpt(self.log_path) + "\n")

    def command(self, *args):
        result = subprocess.run([sys.executable, "cli.py", "--url", self.url, *args, "--json"],
                                cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def poll(self, predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.05)
        self.fail("Condition did not become true before deadline")

    def test_restart_preserves_process_invocation_state_terminal_and_live_chat(self):
        old_token, old_status = self._wait_for_boot()
        self.assertTrue(old_status["restart_supported"])
        pid = self.process.pid
        cwd = os.readlink(f"/proc/{pid}/cwd")
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        upload_marker = self.upload_dir / "restart-marker"
        upload_marker.write_text("preserved", encoding="utf-8")

        workspace = self.command("new", "restart persistence")
        before_message = self.command("send", "--session", workspace["id"],
                                      "message before restart")
        agent = self.command("spawn", "kilo", "--session", workspace["id"],
                             "--cwd", str(self.workdir), "--agent-name", "restart-reviewer",
                             "--history-mode", "none", "--role", "code-reviewer",
                             "--personality", "meticulous")

        def ready_agent():
            row = next(item for item in self.api.get(workspace["id"])["agents"]
                       if item["agent_id"] == agent["agent_id"])
            return row if (row["last_state"] == "running" and
                           (row.get("last_launch") or {}).get("startup_delivery_done")) else None

        ready_agent = self.poll(ready_agent)
        tmux_name = "agentchattr-" + agent["agent_id"]
        self.assertEqual(subprocess.run(["tmux", "has-session", "-t", "=" + tmux_name],
                                       env=self.env, capture_output=True, timeout=5).returncode, 0)
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-p", "-t", "=" + tmux_name, "#{pane_pid}"],
            env=self.env, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        wrapper_pid = ready_agent["last_launch"]["wrapper_pid"]
        launch_nonce = ready_agent["last_launch"]["nonce"]
        agent_token = json.loads((self.data_dir / "identity" /
                                  f"{agent['agent_id']}.json").read_text("utf-8"))["token"]

        async def scenario():
            output = []
            client = cli.ChatClient(self.url, channel=workspace["channel"], output=output.append)
            receiver = asyncio.create_task(client.receive_forever())
            try:
                async with asyncio.timeout(45):
                    await client.ready.wait()
                    old_socket = client.websocket
                    accepted = await asyncio.to_thread(
                        self._request, old_token, "POST", "/api/server/restart",
                        {"instance_id": old_status["instance_id"]})
                    self.assertEqual(accepted["state"], "restarting")
                    new_token, new_status = await asyncio.to_thread(
                        self._wait_for_boot, old_status["instance_id"], 35)
                    while client.websocket is old_socket or not client.ready.is_set():
                        await asyncio.sleep(.05)
                    self.assertNotEqual(new_token, old_token)
                    self.assertNotEqual(new_status["instance_id"], old_status["instance_id"])
                    self.assertEqual(new_status["previous_instance_id"], old_status["instance_id"])
                    self.assertEqual(new_status["state"], "ready")

                    headers = {"Authorization": f"Bearer {agent_token}"}
                    async with httpx.AsyncClient(headers=headers) as http_client:
                        async with streamable_http_client(
                            f"http://127.0.0.1:{self.ports[1]}/mcp", http_client=http_client,
                        ) as (read_stream, write_stream, _):
                            async with ClientSession(read_stream, write_stream) as mcp_session:
                                initialized = await mcp_session.initialize()
                                self.assertIsNotNone(initialized.serverInfo)
                    async with sse_client(
                        f"http://127.0.0.1:{self.ports[2]}/sse", headers=headers,
                        timeout=5, sse_read_timeout=10,
                    ) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as mcp_session:
                            initialized = await mcp_session.initialize()
                            self.assertIsNotNone(initialized.serverInfo)

                    sent = await client.submit("message after restart")
                    self.assertTrue(sent)
                    while not any(message["text"] == "message after restart"
                                  for message in client.messages.values()):
                        await asyncio.sleep(.05)
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)

        asyncio.run(scenario())

        self.assertIsNone(self.process.poll())
        self.assertEqual(self.process.pid, pid)
        self.assertEqual(os.readlink(f"/proc/{pid}/cwd"), cwd)
        self.assertEqual(Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0"), argv)
        self.assertEqual(subprocess.run(["tmux", "has-session", "-t", "=" + tmux_name],
                                       env=self.env, capture_output=True, timeout=5).returncode, 0)
        self.api = WorkspaceAPI(self.url)
        persisted = self.api.get(workspace["id"])
        saved_agent = next(row for row in persisted["agents"] if row["agent_id"] == agent["agent_id"])
        self.assertEqual(saved_agent["profile"], ready_agent["profile"])
        self.assertEqual(saved_agent["profile"]["role"], "code-reviewer")
        self.assertEqual(saved_agent["last_launch"]["wrapper_pid"], wrapper_pid)
        self.assertEqual(saved_agent["last_launch"]["nonce"], launch_nonce)
        os.kill(wrapper_pid, 0)
        self.assertEqual(subprocess.run(
            ["tmux", "display-message", "-p", "-t", "=" + tmux_name, "#{pane_pid}"],
            env=self.env, capture_output=True, text=True, timeout=5,
            check=True).stdout.strip(), pane_pid)
        texts = [row["text"] for row in self.command("read", "--session", workspace["id"])]
        self.assertIn(before_message["text"], texts)
        self.assertIn("message after restart", texts)
        self.assertEqual(upload_marker.read_text("utf-8"), "preserved")

    def test_tui_button_restart_preserves_draft_mode_cursor_and_selection(self):
        self.api = WorkspaceAPI(self.url)
        workspace = self.api.create("tui restart persistence")
        self.addCleanup(self.api.action, workspace["id"], "archive")

        async def scenario():
            client = cli.ChatClient(self.url)
            controller = WorkspaceChatController(
                client, self.api, selector=workspace["id"], no_resume=True,
                data_dir=str(self.data_dir))
            async with application_harness(client, controller, size=(80, 18)) as ui:
                await ui.wait_until(lambda: controller.workspace is not None
                                    and controller.workspace["id"] == workspace["id"], timeout=15)
                await asyncio.wait_for(client.ready.wait(), 15)
                await ui.wait_until(lambda: ui.focused_control == "composer", timeout=15)

                draft = "preserve this unsent restart draft"
                await ui.paste(draft)
                await ui.key("Left")
                await ui.key("Escape")
                before = (ui.view.composer.text, ui.view.composer.buffer.cursor_position,
                          ui.view.composer_mode, controller.workspace["id"])
                self.assertEqual(before[0], draft)
                self.assertEqual(before[1], len(draft) - 1)
                self.assertEqual(before[2], "NORMAL")
                old_socket = client.websocket
                old_status = await asyncio.to_thread(self.api.server_status)

                self.assertTrue(ui.view.focus_named("restart_server"))
                await ui.wait_render()
                await ui.key("Enter")
                await ui.wait_until(lambda: "Restart server?" in ui.screen_text(), timeout=10)
                await ui._send("y")
                await ui.wait_until(
                    lambda: any("Server restarted" in line for line in ui.state.notices.lines),
                    timeout=40)
                await ui.wait_until(lambda: client.ready.is_set()
                                    and client.websocket is not None
                                    and client.websocket is not old_socket, timeout=20)
                successor = await asyncio.to_thread(self.api.server_status)
                self.assertNotEqual(successor["instance_id"], old_status["instance_id"])
                self.assertEqual(successor["previous_instance_id"], old_status["instance_id"])
                self.assertEqual(before, (
                    ui.view.composer.text, ui.view.composer.buffer.cursor_position,
                    ui.view.composer_mode, controller.workspace["id"]))

                self.assertTrue(ui.view.focus_named("composer"))
                await ui.wait_render()
                await ui.send_message()
                await ui.wait_until(lambda: any(message.get("text") == draft
                                                for message in client.messages.values()), timeout=15)
                self.assertEqual(ui.view.composer.text, "")

        asyncio.run(scenario())
        messages = self.command("read", "--session", workspace["id"])
        self.assertEqual(sum(row.get("text") == "preserve this unsent restart draft"
                             for row in messages), 1)


if __name__ == "__main__":
    unittest.main()
