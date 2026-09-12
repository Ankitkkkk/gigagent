"""/api/workspaces against an isolated real server (spec §2 routes)."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

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

    def test_bodiless_create_uses_default_name(self):
        status, workspace = self.call("POST", "/api/workspaces")
        self.assertEqual(status, 200)
        self.assertEqual(workspace["name"], workspace["id"])
        self.call("POST", f"/api/workspaces/{workspace['id']}/archive")

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

    def test_workspace_change_broadcasts_only_changed_workspace(self):
        s, changed = self.call("POST", "/api/workspaces", {"name": "changed-event"})
        self.assertEqual(s, 200)
        s, untouched = self.call("POST", "/api/workspaces", {"name": "untouched-event"})
        self.assertEqual(s, 200)
        client = websocket_connect(
            self.url.replace("http://", "ws://") + f"/ws?token={self.token}", open_timeout=3
        )
        self.addCleanup(client.close)
        s, changed = self.call("PATCH", f"/api/workspaces/{changed['id']}", {"name": "renamed-event"})
        self.assertEqual(s, 200)
        workspace_ids = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                event = json.loads(client.recv(timeout=max(0.001, deadline - time.monotonic())))
            except TimeoutError:
                continue
            if event.get("type") == "workspace":
                workspace_ids.append(event["data"]["id"])
                if event["data"]["id"] == changed["id"]:
                    break
        else:
            self.fail("workspace event not received")
        try:
            while True:
                event = json.loads(client.recv(timeout=0.2))
                if event.get("type") == "workspace":
                    workspace_ids.append(event["data"]["id"])
        except TimeoutError:
            pass
        self.assertEqual(workspace_ids, [changed["id"]])
        self.assertNotIn(untouched["id"], workspace_ids)


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

        self.ws_store.write_identity(ws, none_agent, "none-token")
        self.ws_store.write_identity(ws, literal_agent, "literal-token")

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

    def test_history_repair_does_not_mint_missing_identity_shadow(self):
        ws = self.ws_store.create("missing-shadow")
        for old_mode, mode in (("none", "none"), ("literal", "literal"), ("none", "literal")):
            with self.subTest(old_mode=old_mode, mode=mode):
                agent = self.add_agent(ws, old_mode, None, state="exited")
                result = self.post_history(ws["id"], agent["agent_id"], mode)
                self.assertEqual(result["history_mode"], mode)
                self.assertIsNotNone(result["floor_id"])
                self.assertIsNone(self.ws_store.read_identity(agent["agent_id"]))

    def test_history_repair_preserves_unusable_identity_shadows(self):
        ws = self.ws_store.create("unusable-shadow")
        for mode in ("none", "literal"):
            for shadow in ({}, {"token": ""}, {"token": None}, {"token": 42}, []):
                with self.subTest(mode=mode, shadow=shadow):
                    agent = self.add_agent(ws, "none", None, state="exited")
                    path = self.ws_store.identity_path(agent["agent_id"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    original = json.dumps(shadow)
                    path.write_text(original)
                    result = self.post_history(ws["id"], agent["agent_id"], mode)
                    self.assertEqual(result["history_mode"], mode)
                    self.assertEqual(result["floor_id"], 0)
                    self.assertEqual(path.read_text(), original)

    def test_exited_pending_history_can_be_repaired(self):
        ws = self.ws_store.create("stuck")
        agent = self.add_agent(ws, "literal", None, state="exited")
        self.ws_store.update_agent(ws["id"], agent["agent_id"], history_state="pending")
        result = self.post_history(ws["id"], agent["agent_id"], "literal")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["floor_id"], 0)

    def test_live_pending_history_remains_conflict(self):
        ws = self.ws_store.create("busy")
        for state in ("starting", "running"):
            with self.subTest(state=state):
                agent = self.add_agent(ws, "literal", None, state=state)
                self.ws_store.update_agent(ws["id"], agent["agent_id"], history_state="pending")
                result = self.post_history(ws["id"], agent["agent_id"], "literal")
                self.assertEqual(result.status_code, 409)
                self.assertIsNone(self.ws_store.get_agent(ws["id"], agent["agent_id"])["floor_id"])

    def test_archived_history_rejects_without_mutation(self):
        ws = self.ws_store.create("archived")
        agent = self.add_agent(ws, "none", 7, state="exited")
        before = self.ws_store.get_agent(ws["id"], agent["agent_id"])
        self.ws_store.set_archived(ws["id"], True)
        result = self.post_history(ws["id"], agent["agent_id"], "literal")
        self.assertEqual(getattr(result, "status_code", None), 400)
        self.assertIn("archived", json.loads(result.body)["error"])
        self.assertEqual(self.ws_store.get_agent(ws["id"], agent["agent_id"]), before)
        self.assertIsNone(self.ws_store.read_identity(agent["agent_id"]))

    def test_all_body_routes_reject_malformed_and_nonobject_json(self):
        import httpx
        from fastapi import FastAPI
        ws = self.ws_store.create("json")
        agent = self.add_agent(ws, "none", 0, state="exited")
        # Real handlers and exception handlers; separate ASGI app avoids changing
        # the shared app's middleware lifecycle during later configure() tests.
        isolated = FastAPI(routes=self.app.app.routes, exception_handlers=self.app.app.exception_handlers)
        self.app.workspace_launcher = object()  # invalid bodies must never reach launcher
        base = f"/api/workspaces/{ws['id']}"
        routes = [("POST", "/api/workspaces"), ("PATCH", base), ("POST", base + "/agents"),
                  ("POST", base + f"/agents/{agent['agent_id']}/resume"),
                  ("POST", base + f"/agents/{agent['agent_id']}/history")]

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated, raise_app_exceptions=False),
                                         base_url="http://localhost") as client:
                for method, path in routes:
                    for raw in (b"not json", b"[]", b"null", b"42", b'"text"', b"\xff", b" "):
                        with self.subTest(method=method, path=path, raw=raw):
                            response = await client.request(method, path, content=raw)
                            self.assertEqual(response.status_code, 400, response.text)
                            self.assertEqual(response.json(), {"error": "invalid JSON body"})
        asyncio.run(exercise())

    def test_resume_parses_body_without_content_length_and_allows_empty_body(self):
        class Launcher:
            def resume(self, ws_id, agent_id, fresh, name, cwd):
                return {"fresh": fresh, "name": name, "cwd": cwd}

        self.app.workspace_launcher = Launcher()
        body = {"fresh": True, "name": "reviewer", "cwd": self.tmp}
        result = asyncio.run(self.app.resume_agent("ws_saved", "ag_saved", self.request(body)))
        self.assertEqual(result, body)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
        result = asyncio.run(self.app.resume_agent("ws_saved", "ag_saved", request))
        self.assertEqual(result, {"fresh": False, "name": None, "cwd": None})

    def test_unread_views_fetch_routing_once_and_start_after_marks(self):
        from workspace_launcher import WorkspaceLauncher
        ws = self.ws_store.create("unread-view")
        first = self.add_agent(ws, "literal", 0, state="exited")
        second = self.add_agent(ws, "literal", 0, state="exited")
        ids = [first["agent_id"], second["agent_id"]]
        for text in ("seen", "first pending", "last pending"):
            msg = self.messages.add("user", text, channel=ws["channel"])
            self.ws_store.record_routing(ws["channel"], msg["id"], ids)
        self.ws_store.ack(ws["id"], first["agent_id"], [0])
        self.ws_store.ack(ws["id"], second["agent_id"], [0, 1])
        self.app.workspace_launcher = WorkspaceLauncher(
            store=self.ws_store, messages=self.messages, registry=self.registry, agents=self.triggers,
            config={"agents": {}}, data_dir=Path(self.tmp), root=ROOT,
        )
        for view in ("detail", "unread"):
            with self.subTest(view=view):
                with patch.object(self.ws_store, "routing_for", wraps=self.ws_store.routing_for) as routing:
                    with patch.object(self.messages, "get_since", wraps=self.messages.get_since) as since:
                        result = (self.app._ws_view(self.ws_store.get(ws["id"])) if view == "detail"
                                  else asyncio.run(self.app.workspace_unread(ws["id"])))
                key = "unread_count" if view == "detail" else "count"
                self.assertEqual([a[key] for a in result["agents"]], [2, 1])
                routing.assert_called_once_with(ws["id"])
                self.assertEqual([call.args[0] for call in since.call_args_list], [0, 1])
                if view == "unread":
                    self.assertCountEqual(result["agents"][0]["messages"][0]["routed_to"], ids)

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

    def test_workspace_view_populates_agent_and_hides_routing_bookkeeping(self):
        ws = self.ws_store.create("view")
        agent = self.add_agent(ws, "literal", 0, state="running")
        record = self.ws_store.get(ws["id"])
        record["routing"] = {"4": [agent["agent_id"]]}
        record["routing_done"] = [5]
        record["routing_high_water"] = 3

        class Launcher:
            def unread_for(self, ws_id, agent_id, *, routing=None):
                if ws_id == ws["id"] and agent_id == agent["agent_id"]:
                    return [{"id": 4}, {"id": 5}]
                return []

        self.app.workspace_launcher = Launcher()
        view = self.app._ws_view(record)

        self.assertEqual(view["agents"][0]["unread_count"], 2)
        self.assertEqual(view["agents"][0]["tmux_session"], f"agentchattr-{agent['agent_id']}")
        self.assertNotIn("routing", view)
        self.assertNotIn("routing_done", view)
        self.assertNotIn("routing_high_water", view)

    def _assert_route_yields_while_checkpoint_runs(self, route):
        started = threading.Event()
        release = threading.Event()

        class Launcher:
            def checkpoint(self, ws_id):
                started.set()
                release.wait(timeout=1)
                return {"checked": 0}

        self.app.workspace_launcher = Launcher()

        async def exercise():
            try:
                task = asyncio.create_task(route())
                self.assertTrue(await asyncio.to_thread(started.wait, 0.5))
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                release.set()
                return await task
            finally:
                release.set()

        return asyncio.run(exercise())

    def test_checkpoint_route_keeps_event_loop_responsive(self):
        ws = self.ws_store.create("checkpoint-thread")
        result = self._assert_route_yields_while_checkpoint_runs(
            lambda: self.app.checkpoint_workspace(ws["id"])
        )
        self.assertEqual(result, {"checked": 0})

    def test_archive_route_keeps_event_loop_responsive(self):
        ws = self.ws_store.create("archive-thread")
        result = self._assert_route_yields_while_checkpoint_runs(
            lambda: self.app.archive_workspace(ws["id"])
        )
        self.assertTrue(result["archived"])

    def test_workspace_broadcast_logs_failed_view(self):
        ws = self.ws_store.create("broadcast-error")
        self.add_agent(ws, "literal", 0, state="running")

        class Launcher:
            def unread_for(self, ws_id, agent_id, *, routing=None):
                raise RuntimeError("unread failed")

        self.app.workspace_launcher = Launcher()

        async def exercise():
            saved_loop = self.app._event_loop
            self.app._event_loop = asyncio.get_running_loop()
            try:
                self.app._on_workspace_change()
                await asyncio.sleep(0.02)
            finally:
                self.app._event_loop = saved_loop

        with self.assertLogs(self.app.log, level="ERROR") as logs:
            asyncio.run(exercise())
        self.assertTrue(any("workspace broadcast failed" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
