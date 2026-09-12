"""Real server + real wrapper.py + fake provider in tmux (spec §8)."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cli


@unittest.skipIf(shutil.which("tmux") is None, "tmux not installed")
@unittest.skipIf(sys.platform == "win32", "tmux is Linux/macOS only")
class TmuxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="agentchattr-tmux-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.shim = root / "bin"
        cls.shim.mkdir()
        # Kilo has no adapter, making it a safe stand-in for a provider process.
        kilo = cls.shim / "kilo"
        kilo.write_text("#!/bin/sh\necho 'kilo stub ready'\nwhile :; do sleep 1; done\n")
        kilo.chmod(0o755)
        cls.proj = root / "proj"
        cls.proj.mkdir()
        cls.log = open(root / "server.log", "w+")
        cls.addClassCleanup(cls.log.close)

        socks = [socket.socket() for _ in range(3)]
        for sock in socks:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in socks]
        for sock in socks:
            sock.close()

        cls.url = f"http://127.0.0.1:{ports[0]}"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("AGENTCHATTR_")}
        env["PATH"] = str(cls.shim) + os.pathsep + env.get("PATH", "")
        # All server, wrapper, and test tmux clients use this private socket tree.
        cls.tmux_dir = root / "tmux"
        cls.tmux_dir.mkdir()
        env["TMUX_TMPDIR"] = str(cls.tmux_dir)
        env.pop("TMUX", None)
        cls.tmux_env = dict(env)

        cls.process = subprocess.Popen([
            sys.executable, "run.py", "--port", str(ports[0]),
            "--mcp-http-port", str(ports[1]), "--mcp-sse-port", str(ports[2]),
            "--data-dir", str(root / "data"), "--upload-dir", str(root / "uploads"),
        ], cwd=ROOT, stdout=cls.log, stderr=cls.log, env=env)
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
        # This environment can only address this test's isolated tmux server.
        subprocess.run(
            ["tmux", "kill-server"], capture_output=True, timeout=5, env=cls.tmux_env
        )
        if cls.process.poll() is None:
            cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, method=method, data=data)
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Session-Token", self.token)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def wait_state(self, ws_id, agent_id, state, timeout=45):
        deadline = time.time() + timeout
        agent = None
        while time.time() < deadline:
            status, workspace = self.call("GET", f"/api/workspaces/{ws_id}")
            self.assertEqual(status, 200, workspace)
            agent = next(item for item in workspace["agents"]
                         if item["agent_id"] == agent_id)
            if agent["last_state"] == state:
                return agent
            time.sleep(0.5)
        self.fail(f"agent never reached {state}: {agent}")

    def tmux_alive(self, name):
        return subprocess.run(
            ["tmux", "has-session", "-t", name], capture_output=True,
            timeout=5, env=self.tmux_env,
        ).returncode == 0

    def test_spawn_stop_fresh_lifecycle(self):
        status, workspace = self.call("POST", "/api/workspaces", {"name": "tmux"})
        self.assertEqual(status, 200, workspace)
        status, agent = self.call(
            "POST", f"/api/workspaces/{workspace['id']}/agents",
            {"provider": "kilo", "cwd": str(self.proj), "history_mode": "none"},
        )
        self.assertEqual(status, 200, agent)
        self.assertEqual(agent["registry_name"], "kilo-1")

        running = self.wait_state(workspace["id"], agent["agent_id"], "running")
        tmux_name = f"agentchattr-{agent['agent_id']}"
        self.assertTrue(self.tmux_alive(tmux_name))
        self.assertIsNotNone(running["last_launch"]["pid"])

        status, error = self.call(
            "POST", f"/api/workspaces/{workspace['id']}/agents/{agent['agent_id']}/resume", {}
        )
        self.assertEqual(status, 409, error)

        status, stopped = self.call(
            "POST", f"/api/workspaces/{workspace['id']}/agents/{agent['agent_id']}/stop"
        )
        self.assertEqual(status, 200, stopped)
        self.assertEqual(stopped["last_state"], "exited")
        deadline = time.monotonic() + 10
        while self.tmux_alive(tmux_name) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(self.tmux_alive(tmux_name))

        status, error = self.call(
            "POST", f"/api/workspaces/{workspace['id']}/agents/{agent['agent_id']}/resume", {}
        )
        self.assertEqual(status, 409, error)
        self.assertIn("--fresh", error["error"])

        status, restarted = self.call(
            "POST", f"/api/workspaces/{workspace['id']}/agents/{agent['agent_id']}/resume",
            {"fresh": True},
        )
        self.assertEqual(status, 200, restarted)
        self.assertEqual(restarted["last_launch"]["kind"], "fresh")
        self.wait_state(workspace["id"], agent["agent_id"], "running")
        self.assertTrue(self.tmux_alive(tmux_name))

        status, archived = self.call("POST", f"/api/workspaces/{workspace['id']}/archive")
        self.assertEqual(status, 200, archived)
        self.wait_state(workspace["id"], agent["agent_id"], "exited")
        self.assertFalse(self.tmux_alive(tmux_name))


if __name__ == "__main__":
    unittest.main()
