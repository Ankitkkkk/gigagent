"""/api/workspaces against an isolated real server (spec §2 routes)."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from starlette.requests import Request
from websockets.sync.client import connect as websocket_connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cli  # for fetch_session_token


class WorkspaceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="agentchattr-ws-api-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.log = open(Path(cls.temp.name) / "server.log", "w+")
        cls.addClassCleanup(cls.log.close)
        socks = [socket.socket() for _ in range(3)]
        for s in socks:
            s.bind(("127.0.0.1", 0))
        ports = [s.getsockname()[1] for s in socks]
        for s in socks:
            s.close()
        cls.url = f"http://127.0.0.1:{ports[0]}"
        cls.data_dir = Path(cls.temp.name) / "data"
        cls.process = subprocess.Popen([
            sys.executable, "run.py", "--port", str(ports[0]), "--mcp-http-port", str(ports[1]),
            "--mcp-sse-port", str(ports[2]), "--data-dir", str(cls.data_dir),
            "--upload-dir", cls.temp.name + "/uploads",
        ], cwd=ROOT, stdout=cls.log, stderr=cls.log,
            env={k: v for k, v in os.environ.items() if not k.startswith("AGENTCHATTR_")})
        cls.addClassCleanup(cls.stop_server)
        for _ in range(100):
            if cls.process.poll() is not None:
                raise RuntimeError("server exited during startup")
            try:
                cls.token = cli.fetch_session_token(cls.url)
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    @classmethod
    def stop_server(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()

    def call(self, method, path, body=None, bearer=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, method=method, data=data)
        req.add_header("Content-Type", "application/json")
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        else:
            req.add_header("X-Session-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_crud_archive_and_order(self):
        s, a = self.call("POST", "/api/workspaces", {"name": "alpha"})
        self.assertEqual(s, 200); self.assertTrue(a["id"].startswith("ws_"))
        s, b = self.call("POST", "/api/workspaces", {})
        self.assertEqual(b["name"], b["id"])
        s, a2 = self.call("PATCH", f"/api/workspaces/{a['id']}", {"name": "alpha2"})
        self.assertEqual(a2["name"], "alpha2")
        s, lst = self.call("GET", "/api/workspaces")
        self.assertEqual([w["id"] for w in lst["workspaces"]][:2], [a["id"], b["id"]])
        s, _ = self.call("POST", f"/api/workspaces/{b['id']}/archive")
        s, lst = self.call("GET", "/api/workspaces")
        self.assertNotIn(b["id"], [w["id"] for w in lst["workspaces"]])
        s, lst = self.call("GET", "/api/workspaces?include_archived=1")
        self.assertIn(b["id"], [w["id"] for w in lst["workspaces"]])
        s, _ = self.call("POST", f"/api/workspaces/{b['id']}/unarchive")
        s, w = self.call("GET", f"/api/workspaces/{b['id']}")
        self.assertFalse(w["archived"])
        s, _ = self.call("GET", "/api/workspaces/ws_nope")
        self.assertEqual(s, 404)
        s, settings = self.call("GET", "/api/settings")
        self.assertIn(a["channel"], settings["channels"])
        self.assertLessEqual(len(a["channel"]), 20)

    def test_spawn_validation_errors_are_400_with_text(self):
        s, ws = self.call("POST", "/api/workspaces", {"name": "v"})
        s, err = self.call("POST", f"/api/workspaces/{ws['id']}/agents",
                           {"provider": "nope", "cwd": self.temp.name, "history_mode": "none"})
        self.assertEqual(s, 400); self.assertIn("unknown provider", err["error"])
        s, err = self.call("POST", f"/api/workspaces/{ws['id']}/agents",
                           {"provider": "claude", "cwd": "relative", "history_mode": "none"})
        self.assertEqual(s, 400); self.assertIn("absolute", err["error"])
        s, err = self.call("POST", f"/api/workspaces/{ws['id']}/agents",
                           {"provider": "claude", "cwd": self.temp.name, "history_mode": "summary"})
        self.assertEqual(s, 400); self.assertIn("not available", err["error"])
        self.call("POST", f"/api/workspaces/{ws['id']}/archive")
        s, err = self.call("POST", f"/api/workspaces/{ws['id']}/agents",
                           {"provider": "claude", "cwd": self.temp.name, "history_mode": "none"})
        self.assertEqual(s, 400); self.assertIn("archived", err["error"])
        s, w = self.call("GET", f"/api/workspaces/{ws['id']}")
        self.assertEqual(w["agents"], [])

    def test_unread_retry_checkpoint_on_empty_workspace(self):
        s, ws = self.call("POST", "/api/workspaces", {"name": "u"})
        s, out = self.call("GET", f"/api/workspaces/{ws['id']}/unread")
        self.assertEqual(out["agents"], [])
        s, out = self.call("POST", f"/api/workspaces/{ws['id']}/agents/ag_none/retry")
        self.assertEqual(s, 404)
        s, out = self.call("POST", f"/api/workspaces/{ws['id']}/checkpoint")
        self.assertEqual(out["checked"], 0)

    def test_status_has_data_dir_and_export_refuses_agents(self):
        s, st = self.call("GET", "/api/status")
        self.assertEqual(st["data_dir"], str(self.data_dir.resolve()))
        req = urllib.request.Request(self.url + "/api/register", method="POST",
                                     data=json.dumps({"base": "claude"}).encode())
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as r:
            token = json.loads(r.read())["token"]
        s, err = self.call("GET", "/api/export", bearer=token)
        self.assertEqual(s, 403)
        s, msgs = self.call("GET", "/api/messages?limit=5", bearer=token)
        self.assertEqual(s, 200)

    def test_workspace_change_broadcasts_workspace_view(self):
        client = websocket_connect(
            self.url.replace("http://", "ws://") + f"/ws?token={self.token}", open_timeout=3
        )
        self.addCleanup(client.close)
        s, created = self.call("POST", "/api/workspaces", {"name": "events"})
        self.assertEqual(s, 200)
        for _ in range(20):
            event = json.loads(client.recv(timeout=3))
            if event.get("type") == "workspace" and event.get("data", {}).get("id") == created["id"]:
                break
        else:
            self.fail("workspace event not received")
        self.assertNotIn("routing", event["data"])


class WorkspaceHistoryRouteTests(unittest.TestCase):
    def setUp(self):
        import app as app_module
        from agents import AgentTrigger
        from registry import RuntimeRegistry
        from store import MessageStore
        from workspace_store import WorkspaceStore

        self.app = app_module
        self.tmp = tempfile.mkdtemp(prefix="agentchattr-ws-history-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        data = Path(self.tmp)
        self.ws_store = WorkspaceStore(data / "workspaces.json", data / "identity")
        self.messages = MessageStore(str(data / "messages.jsonl"))
        self.registry = RuntimeRegistry(data_dir=self.tmp)
        self.registry.seed({"fake": {"command": "fake", "label": "Fake", "color": "#123456"}})
        self.triggers = AgentTrigger(self.registry, data_dir=self.tmp)
        saved = (app_module.workspace_store, app_module.store, app_module.agents,
                 app_module.workspace_launcher)
        app_module.workspace_store = self.ws_store
        app_module.store = self.messages
        app_module.agents = self.triggers
        app_module.workspace_launcher = None
        self.addCleanup(self._restore, saved)

    def _restore(self, saved):
        (self.app.workspace_store, self.app.store, self.app.agents,
         self.app.workspace_launcher) = saved

    def request(self, body):
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        return Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)

    def add_agent(self, ws, mode, floor, state="done"):
        slot = len(self.ws_store.get(ws["id"])["agents"]) + 1
        agent = self.ws_store.add_agent(
            ws["id"], provider="fake", cwd=self.tmp, history_mode=mode,
            registry_name=f"fake-{slot}", floor_id=floor,
            native_session_id="sid", history_state="done",
            last_launch={"kind": "spawn", "nonce": "old", "at": "2026-09-12T00:00:00Z", "pid": None},
        )
        return self.ws_store.update_agent(ws["id"], agent["agent_id"], last_state=state)

    def post_history(self, ws_id, agent_id, mode):
        return asyncio.run(self.app.change_history_mode(
            ws_id, agent_id, self.request({"mode": mode})
        ))

    def test_same_mode_repairs_missing_floor_and_restores_reads(self):
        ws = self.ws_store.create("repair")
        self.messages.add("user", "general only", channel="general")
        self.messages.add("user", "old one", channel=ws["channel"])
        self.messages.add("user", "old two", channel=ws["channel"])
        none_agent = self.add_agent(ws, "none", None, state="running")
        literal_agent = self.add_agent(ws, "literal", None, state="running")

        repaired_none = self.post_history(ws["id"], none_agent["agent_id"], "none")
        repaired_literal = self.post_history(ws["id"], literal_agent["agent_id"], "literal")

        self.assertEqual(repaired_none["floor_id"], 3)
        self.assertEqual(repaired_literal["floor_id"], 0)
        self.assertEqual(self.ws_store.read_identity(none_agent["agent_id"])["floor_id"], 3)
        self.assertEqual(self.ws_store.read_identity(literal_agent["agent_id"])["floor_id"], 0)
        new_message = self.messages.add("user", "new work", channel=ws["channel"])
        channel_messages = self.messages.get_recent(50, channel=ws["channel"])
        visible = self.app._filter_messages_for_agent("fake-1", channel_messages)
        self.assertEqual([m["id"] for m in visible], [new_message["id"]])

    def test_stopped_widening_catches_up_once_after_ready(self):
        from providers.base import NullAdapter
        from workspace_launcher import WorkspaceLauncher

        ws = self.ws_store.create("widen")
        agent = self.add_agent(ws, "none", 0, state="exited")
        changed = self.post_history(ws["id"], agent["agent_id"], "literal")
        self.assertEqual(changed["history_state"], "pending")
        self.assertFalse((Path(self.tmp) / "fake-1_queue.jsonl").exists())

        launcher = WorkspaceLauncher(
            store=self.ws_store, messages=self.messages, registry=self.registry,
            agents=self.triggers, config={"agents": {"fake": {}}}, data_dir=Path(self.tmp),
            root=ROOT, adapters=lambda name, cfg: NullAdapter(name, cfg), sleep=lambda _: None,
            background=lambda fn, *args: fn(*args),
        )
        self.app.workspace_launcher = launcher
        first_launch = {"kind": "resume", "nonce": "ready-1", "at": "2026-09-12T00:01:00Z", "pid": None}
        self.ws_store.update_agent(ws["id"], agent["agent_id"], last_state="starting", last_launch=first_launch)
        launcher.on_heartbeat("fake-1", ready=True, pid=123)

        queue = Path(self.tmp) / "fake-1_queue.jsonl"
        entries = [json.loads(line) for line in queue.read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertIn("since_id=-1", entries[0]["prompt"])
        self.assertEqual(self.ws_store.get_agent(ws["id"], agent["agent_id"])["history_state"], "done")

        second_launch = {"kind": "resume", "nonce": "ready-2", "at": "2026-09-12T00:02:00Z", "pid": None}
        self.ws_store.update_agent(ws["id"], agent["agent_id"], last_state="starting", last_launch=second_launch)
        launcher.on_heartbeat("fake-1", ready=True, pid=456)
        entries = [json.loads(line) for line in queue.read_text().splitlines()]
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
