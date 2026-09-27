"""Real run.py HTTP/MCP orchestration with inert provider processes."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

try:
    from _cli_server import IsolatedCliServer, isolated_environment
except ModuleNotFoundError:
    from tests._cli_server import IsolatedCliServer, isolated_environment


@unittest.skipUnless(shutil.which("tmux") and sys.platform != "win32", "Requires Unix tmux")
class OrchestrationIntegrationTests(IsolatedCliServer):
    @classmethod
    def environment_additions(cls):
        shim_dir = Path(cls.temp.name) / "bin"
        shim_dir.mkdir()
        script = (f"#!{sys.executable}\n"
                  "import sys\n"
                  "print('INERT PROVIDER READY', flush=True)\n"
                  "for line in sys.stdin:\n"
                  "    print('INERT PROVIDER RECEIVED', flush=True)\n")
        for provider in ("claude", "codex", "gemini", "agy", "kimi", "qwen", "kilo",
                         "codebuddy", "copilot"):
            path = shim_dir / provider
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        additions = {"PATH": str(shim_dir) + os.pathsep + os.defpath}
        cleanup_env = isolated_environment(cls.temp.name, additions)
        cls.addClassCleanup(subprocess.run, ['tmux', 'kill-server'], env=cleanup_env,
                            capture_output=True, timeout=5)
        return additions

    def setUp(self):
        self.workdir = Path(self.temp.name) / self.id().rsplit(".", 1)[-1]
        self.workdir.mkdir()

    def ready_agent(self, ws_id, agent_id):
        def current():
            workspace = self.api.get(ws_id)
            agent = next(row for row in workspace["agents"] if row["agent_id"] == agent_id)
            launch = agent.get("last_launch") or {}
            return agent if (agent["last_state"] == "running"
                             and launch.get("startup_delivery_done")) else None
        return self.poll(current, timeout=25)

    def identity(self, agent_id):
        return json.loads((self.data_dir / "identity" /
                           f"{agent_id}.json").read_text("utf-8"))

    def mcp_call(self, token, action="pending", **arguments):
        async def call():
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{self.ports[1]}/mcp", http_client=client,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "chat_orchestrate", {"action": action, **arguments})
                        return result.content[0].text
        return asyncio.run(call())

    def new_routed_session(self, name):
        ws = self.json_command("new", name, "--orchestrator-provider", "kilo",
                               "--cwd", str(self.workdir))
        manager = next(row for row in ws["agents"] if row["kind"] == "orchestrator")
        self.ready_agent(ws["id"], manager["agent_id"])
        worker = self.json_command(
            "spawn", "kilo", "--session", ws["id"], "--cwd", str(self.workdir),
            "--agent-name", name.replace(" ", "-") + "-reviewer", "--history-mode", "none",
            "--role", "code-reviewer", "--personality", "meticulous")
        self.ready_agent(ws["id"], worker["agent_id"])
        return self.api.get(ws["id"]), manager, worker

    def test_authenticated_pending_route_is_idempotent_and_session_scoped(self):
        ws, manager, worker = self.new_routed_session("mcp route")
        message = self.json_command("send", "--session", ws["id"],
                                    "Review the authentication change")
        manager_token = self.identity(manager["agent_id"])["token"]

        pending = json.loads(self.mcp_call(manager_token))
        self.assertEqual(pending["requests"][0]["message"]["id"], message["id"])
        roster = {row["agent_id"]: row for row in pending["workers"]}
        self.assertEqual(roster[worker["agent_id"]]["profile"]["role"], "code-reviewer")
        self.assertEqual(roster[worker["agent_id"]]["profile"]["personality"], "meticulous")

        args = {"message_id": message["id"], "agent_ids": [worker["agent_id"]],
                "reason": "Saved reviewer profile matches"}
        first = json.loads(self.mcp_call(manager_token, "route", **args))
        repeated = json.loads(self.mcp_call(manager_token, "route", **args))
        self.assertEqual(first["status"], "queued")
        self.assertEqual(repeated, first)
        ledger = json.loads((self.data_dir / "orchestration.json").read_text("utf-8"))
        record = ledger["requests"][f"{ws['id']}:{message['id']}"]
        self.assertEqual(record["agent_ids"], [worker["agent_id"]])
        self.assertEqual(record["deliveries"], {worker["agent_id"]: "queued"})
        notices = [row for row in self.json_command("read", "--session", ws["id"])
                   if row.get("metadata", {}).get("orchestration")]
        self.assertEqual(len(notices), 1)

        worker_error = self.mcp_call(self.identity(worker["agent_id"])["token"])
        self.assertIn("Error:", worker_error)
        other, other_manager, _ = self.new_routed_session("other route")
        cross_error = self.mcp_call(self.identity(other_manager["agent_id"])["token"],
                                    "route", **args)
        self.assertIn("No pending routing request", cross_error)
        self.assertNotEqual(other["id"], ws["id"])

    def test_mentions_pause_and_profile_resume_fresh_contract(self):
        ws, manager, worker = self.new_routed_session("lifecycle route")
        manager_queue = self.data_dir / f"{manager['registry_name']}_queue.jsonl"
        explicit = self.json_command("send", "--session", ws["id"],
                                     f"@{worker['registry_name']} inspect directly")
        ledger_path = self.data_dir / "orchestration.json"
        if ledger_path.exists():
            ledger = json.loads(ledger_path.read_text("utf-8"))["requests"]
            self.assertNotIn(f"{ws['id']}:{explicit['id']}", ledger)

        original_profile = worker["profile"]
        self.api.action(ws["id"], "stop", worker["agent_id"])
        resumed = self.api.action(ws["id"], "resume", worker["agent_id"], body={"fresh": True})
        self.ready_agent(ws["id"], worker["agent_id"])
        self.assertEqual(resumed["profile"], original_profile)
        self.api.action(ws["id"], "stop", worker["agent_id"])
        fresh = self.api.action(ws["id"], "resume", worker["agent_id"], body={"fresh": True})
        self.ready_agent(ws["id"], worker["agent_id"])
        self.assertEqual(fresh["profile"], original_profile)
        self.assertEqual(self.identity(worker["agent_id"])["profile"], original_profile)

        self.api.action(ws["id"], "stop", manager["agent_id"])
        stopped = self.api.get(ws["id"])
        self.assertFalse(stopped["orchestrator"]["enabled"])
        before = manager_queue.read_text("utf-8") if manager_queue.exists() else ""
        waiting = self.json_command("send", "--session", ws["id"],
                                    "Keep this request pending while paused")
        key = f"{ws['id']}:{waiting['id']}"
        self.poll(lambda: ledger_path.exists() and
                  json.loads(ledger_path.read_text("utf-8"))["requests"].get(key))
        record = json.loads(ledger_path.read_text("utf-8"))["requests"][key]
        self.assertEqual(record["status"], "pending")
        current = self.api.get(ws["id"])
        current_manager = next(row for row in current["agents"]
                               if row["agent_id"] == manager["agent_id"])
        self.assertEqual(current_manager["last_state"], "exited")
        self.assertFalse(current["orchestrator"]["enabled"])
        after = manager_queue.read_text("utf-8") if manager_queue.exists() else ""
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
