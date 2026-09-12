"""app.py hooks for workspaces: rename/deregister propagation, HTTP filtering (spec §1, §2, §7)."""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

import app as app_module
import mcp_bridge
from _workspace_helpers import app_cfg as cfg


class HooksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved_hooks = (mcp_bridge.workspace_policy, mcp_bridge.workspace_ack)
        saved_presence = dict(mcp_bridge._presence)
        def restore_hooks():
            mcp_bridge.workspace_policy, mcp_bridge.workspace_ack = saved_hooks
            mcp_bridge._presence.clear()
            mcp_bridge._presence.update(saved_presence)
        self.addCleanup(restore_hooks)
        app_module.configure(cfg(self.tmp))
        self.ws_store = app_module.workspace_store
        self.ws = self.ws_store.create("proj")
        self.agent = self.ws_store.add_agent(self.ws["id"], provider="claude", cwd="/p",
            history_mode="none", registry_name="claude-1", floor_id=3, native_session_id="sid",
            history_state="done", last_launch={"kind": "spawn", "nonce": "n", "at": "t", "pid": None})
        self.ws_store.update_agent(self.ws["id"], self.agent["agent_id"], last_state="running")
        self.reg = app_module.registry.register("claude", preferred_name="claude-1")
        for i in range(6):
            app_module.store.add("ankit", f"m{i}", channel=self.ws["channel"])

    def test_filter_messages_for_member_applies_floor(self):
        app_module.store.add("system", "private", msg_type="summary", channel=self.ws["channel"],
                             metadata={"audience": [self.agent["agent_id"]]})   # id 6
        msgs = app_module.store.get_recent(50, channel=self.ws["channel"])
        out = app_module._filter_messages_for_agent("claude-1", msgs)
        self.assertEqual([m["id"] for m in out], [3, 4, 5, 6])
        out = app_module._filter_messages_for_agent("gemini", msgs)
        self.assertEqual([m["id"] for m in out], [0, 1, 2, 3, 4, 5])   # non-member: no floor, no private

    def test_filter_blocks_when_floor_lost(self):
        self.ws_store.update_agent(self.ws["id"], self.agent["agent_id"], floor_id=None)
        msgs = app_module.store.get_recent(50, channel=self.ws["channel"])
        self.assertIsNone(app_module._filter_messages_for_agent("claude-1", msgs))

    def test_rename_propagates_to_workspace_record(self):
        app_module._propagate_agent_rename("claude-1", "reviewer")
        self.assertEqual(self.ws_store.get_agent(self.ws["id"], self.agent["agent_id"])["registry_name"], "reviewer")

    def test_deregister_marks_exited(self):
        app_module._on_agent_deregistered("claude-1")
        self.assertEqual(self.ws_store.get_agent(self.ws["id"], self.agent["agent_id"])["last_state"], "exited")

    def test_mcp_bridge_hooks_are_wired_by_wire_workspace_hooks(self):
        app_module.wire_workspace_hooks()
        self.assertIsNotNone(mcp_bridge.workspace_policy("claude-1", self.ws["channel"]))
        self.assertIsNone(mcp_bridge.workspace_policy("claude-1", "general"))
        self.ws_store.record_routing(self.ws["channel"], 4, [self.agent["agent_id"]])
        mcp_bridge.workspace_ack("claude-1", self.ws["channel"], [4])
        self.assertEqual(self.ws_store.get_agent(self.ws["id"], self.agent["agent_id"])["read_mark"], 4)


    def request(self, token=None, body=None):
        headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
        async def receive():
            return {"type": "http.request", "body": json.dumps(body or {}).encode(), "more_body": False}
        return Request({"type": "http", "method": "GET", "path": "/", "headers": headers}, receive)

    def test_messages_endpoint_filters_agent_and_preserves_browser_read(self):
        token = self.reg["token"]
        agent_result = asyncio.run(app_module.get_messages(self.request(token), channel=self.ws["channel"]))
        self.assertEqual([m["id"] for m in agent_result], [3, 4, 5])
        browser_result = asyncio.run(app_module.get_messages(self.request(), channel=self.ws["channel"]))
        self.assertEqual([m["id"] for m in browser_result], [0, 1, 2, 3, 4, 5])
        self.ws_store.update_agent(self.ws["id"], self.agent["agent_id"], floor_id=None)
        blocked = asyncio.run(app_module.get_messages(self.request(token), channel=self.ws["channel"]))
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("/history", json.loads(blocked.body)["error"])

    def test_export_endpoint_refuses_agent_and_status_reports_directory(self):
        response = asyncio.run(app_module.export_history(self.request(self.reg["token"])))
        self.assertEqual(response.status_code, 403)
        self.assertIn("browser-only", json.loads(response.body)["error"])
        status = asyncio.run(app_module.get_status())
        self.assertEqual(status["data_dir"], str(Path(self.tmp).resolve()))

    def test_ready_heartbeat_uses_authenticated_identity(self):
        class Launcher:
            def __init__(self):
                self.calls = []
            def on_heartbeat(self, name, ready, pid):
                self.calls.append((name, ready, pid))
        previous = app_module.workspace_launcher
        launcher = Launcher()
        app_module.workspace_launcher = launcher
        self.addCleanup(setattr, app_module, "workspace_launcher", previous)
        response = asyncio.run(app_module.heartbeat("different-name", self.request(self.reg["token"],
                                                                                 {"ready": True, "pid": 123})))
        self.assertEqual(response["name"], "claude-1")
        self.assertEqual(launcher.calls, [("claude-1", True, 123)])


if __name__ == "__main__":
    unittest.main()
