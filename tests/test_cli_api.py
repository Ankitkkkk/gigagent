"""Authenticated CLI HTTP transport and workspace resolution."""

from contextlib import contextmanager
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli_api import CLIError, fetch_session_token, request_json
from cli_workspaces import WorkspaceAPI, resolve_agent, resolve_session


class _Server(ThreadingHTTPServer):
    daemon_threads = True


@contextmanager
def serve(handler):
    server = _Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def send_json(self, status, value):
        data = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ResolverTests(unittest.TestCase):
    def test_session_resolution_precedence_and_ambiguous_prefix(self):
        rows = [dict(id="ws_a", name="billing", agents=[]),
                dict(id="ws_b", name="billing tools", agents=[])]
        self.assertEqual(resolve_session(rows, "ws_b")["id"], "ws_b")
        self.assertEqual(resolve_session(rows, "billing")["id"], "ws_a")
        with self.assertRaisesRegex(CLIError, "ws_a.*ws_b"):
            resolve_session(rows, "bill")

    def test_duplicate_exact_session_names_are_ambiguous_and_sanitized(self):
        rows = [dict(id="ws_a\x1b[31m", name="same\nname", agents=[]),
                dict(id="ws_b", name="same\nname", agents=[])]
        with self.assertRaises(CLIError) as caught:
            resolve_session(rows, "same\nname")
        message = caught.exception.message
        self.assertIn("ws_a[31m", message)
        self.assertIn("ws_b", message)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\n", message)

    def test_unknown_session_selector_has_clear_error(self):
        with self.assertRaisesRegex(CLIError, "session.*missing"):
            resolve_session([], "missing")

    def test_agent_resolution_uses_exact_name_or_id_then_unique_provider(self):
        workspace = {"agents": [
            dict(agent_id="ag_a", registry_name="claude-1", provider="claude"),
            dict(agent_id="ag_b", registry_name="codex-1", provider="codex"),
        ]}
        self.assertEqual(resolve_agent(workspace, "claude-1")["agent_id"], "ag_a")
        self.assertEqual(resolve_agent(workspace, "ag_b")["agent_id"], "ag_b")
        self.assertEqual(resolve_agent(workspace, "claude")["agent_id"], "ag_a")

    def test_agent_resolution_rejects_ambiguous_provider_and_unknown_selector(self):
        workspace = {"agents": [
            dict(agent_id="ag_a", registry_name="claude-1", provider="claude"),
            dict(agent_id="ag_b", registry_name="claude-2", provider="claude"),
        ]}
        with self.assertRaises(CLIError) as caught:
            resolve_agent(workspace, "claude")
        self.assertRegex(caught.exception.message, "claude-1.*ag_a.*claude-2.*ag_b")
        with self.assertRaisesRegex(CLIError, "agent.*codex"):
            resolve_agent(workspace, "codex")


class RequestJsonTests(unittest.TestCase):
    def test_json_post_patch_and_bodiless_post_preserve_method_headers_and_body(self):
        seen = []

        class Handler(_QuietHandler):
            def record(self):
                length = int(self.headers.get("Content-Length", "0"))
                seen.append((self.command, self.path, self.headers, self.rfile.read(length)))
                self.send_json(200, {})

            do_POST = record
            do_PATCH = record

        with serve(Handler) as url:
            self.assertEqual(request_json(url, "secret-token", "POST", "/one", {"x": 1}), {})
            self.assertEqual(request_json(url, "secret-token", "PATCH", "/two", {"x": 2}), {})
            self.assertEqual(request_json(url, "secret-token", "POST", "/three"), {})

        self.assertEqual([(m, p, b) for m, p, _, b in seen], [
            ("POST", "/one", b'{"x": 1}'),
            ("PATCH", "/two", b'{"x": 2}'),
            ("POST", "/three", b""),
        ])
        for _, _, headers, _ in seen:
            self.assertEqual(headers["X-Session-Token"], "secret-token")
        self.assertEqual(seen[0][2]["Content-Type"], "application/json")
        self.assertEqual(seen[1][2]["Content-Type"], "application/json")

    def test_empty_successful_response_returns_empty_object(self):
        class Handler(_QuietHandler):
            def do_GET(self):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        with serve(Handler) as url:
            self.assertEqual(request_json(url, "token", "GET", "/empty"), {})

    def test_http_error_messages_and_status_are_preserved(self):
        class Handler(_QuietHandler):
            def do_GET(self):
                if self.path == "/error":
                    self.send_json(400, {"error": "bad request"})
                elif self.path == "/detail":
                    self.send_json(422, {"detail": [
                        {"loc": ["body", "name"], "msg": "field required"},
                        {"loc": ["body", "options", 0], "msg": "invalid value"},
                    ]})
                elif self.path == "/missing-session":
                    self.send_json(404, {"error": "session not found"})
                elif self.path == "/missing-agent":
                    self.send_json(404, {"error": "agent not found"})
                else:
                    self.send_json(503, {"error": "agent launching is not available on this server"})

        with serve(Handler) as url:
            cases = [
                ("/error", 400, "bad request"),
                ("/detail", 422,
                 "name: field required; options.0: invalid value"),
                ("/missing-session", 404, "session not found"),
                ("/missing-agent", 404, "agent not found"),
                ("/launch", 503, "agent launching is not available on this server"),
            ]
            for path, status, message in cases:
                with self.subTest(path=path), self.assertRaises(CLIError) as caught:
                    request_json(url, "token", "GET", path)
                self.assertEqual(caught.exception.status, status)
                self.assertIn(message, caught.exception.message)
                if path == "/detail":
                    self.assertNotIn("{", caught.exception.message)
                    self.assertNotIn('"msg"', caught.exception.message)

    def test_non_json_http_error_network_error_and_timeout_do_not_leak_secrets(self):
        class Handler(_QuietHandler):
            def do_GET(self):
                data = b"upstream exploded at http://secret.invalid/?token=leaked"
                self.send_response(500)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with serve(Handler) as url:
            with self.assertRaises(CLIError) as caught:
                request_json(url, "secret-token", "GET", "/bad")
            self.assertEqual(caught.exception.status, 500)
            self.assertNotIn("secret.invalid", caught.exception.message)
            self.assertNotIn("secret-token", caught.exception.message)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        unused_port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(CLIError) as caught:
            request_json(f"http://127.0.0.1:{unused_port}", "secret-token",
                         "GET", "/?token=path-secret", timeout=0.1)
        self.assertIsNone(caught.exception.status)
        self.assertNotIn("secret-token", caught.exception.message)
        self.assertNotIn("path-secret", caught.exception.message)

        class TimeoutOpener:
            def open(self, _request, timeout):
                raise TimeoutError(f"timeout={timeout}; token=secret-token")

        with patch("cli_api.build_opener", return_value=TimeoutOpener()):
            with self.assertRaises(CLIError) as caught:
                request_json("http://127.0.0.1:8300", "secret-token",
                             "GET", "/timeout", timeout=0.25)
        self.assertEqual(caught.exception.message,
                         "Could not connect to the local yapp server")

    def test_redirect_is_rejected_before_credentials_reach_target(self):
        reached = []

        class Target(_QuietHandler):
            def do_GET(self):
                reached.append((self.path, self.headers.get("X-Session-Token")))
                self.send_json(200, {"wrong": True})

        with serve(Target) as target_url:
            class Redirect(_QuietHandler):
                def do_GET(self):
                    self.send_response(307)
                    self.send_header("Location", target_url + "/stolen")
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            with serve(Redirect) as url:
                with self.assertRaisesRegex(CLIError, "redirect"):
                    request_json(url, "secret-token", "GET", "/redirect")
        self.assertEqual(reached, [])


class WorkspaceApiTests(unittest.TestCase):
    def test_api_quotes_paths_and_uses_literal_query_values(self):
        seen = []

        class Handler(_QuietHandler):
            def do_GET(self):
                seen.append(self.path)
                if self.path == "/":
                    data = b'<script>window.__SESSION_TOKEN__="token";</script>'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path.startswith("/api/workspaces?"):
                    self.send_json(200, {"workspaces": []})
                else:
                    self.send_json(200, {})

            def do_POST(self):
                seen.append(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_json(200, {})

        with serve(Handler) as url:
            api = WorkspaceAPI(url)
            api.list(False)
            api.list(True)
            api.get("ws /?x")
            api.action("ws /", "history", agent_id="ag /", body={"mode": "literal"})
            api.unread("ws /", "ag /?")
        self.assertIn("/api/workspaces?include_archived=0", seen)
        self.assertIn("/api/workspaces?include_archived=1", seen)
        self.assertIn("/api/workspaces/ws%20%2F%3Fx", seen)
        self.assertIn("/api/workspaces/ws%20%2F/agents/ag%20%2F/history", seen)
        self.assertIn("/api/workspaces/ws%20%2F/unread?agent_id=ag+%2F%3F", seen)

    def test_api_maps_workspace_operations_to_http_contract(self):
        seen = []

        class Handler(_QuietHandler):
            def reply(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                seen.append((self.command, self.path, json.loads(body) if body else None))
                self.send_json(200, {"ok": True})

            def do_GET(self):
                if self.path == "/":
                    data = b'<script>window.__SESSION_TOKEN__="token";</script>'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.reply()

            do_POST = reply
            do_PATCH = reply

        with serve(Handler) as url:
            api = WorkspaceAPI(url)
            api.create("billing")
            api.rename("ws_a", "tools")
            api.action("ws_a", "spawn", body={"provider": "claude"})
            api.action("ws_a", "archive")
            api.action("ws_a", "unarchive")
            api.action("ws_a", "checkpoint")
            api.action("ws_a", "stop", agent_id="ag_a", body={"ignored": True})
            api.action("ws_a", "retry", agent_id="ag_a", body={"ignored": True})
            api.action("ws_a", "resume", agent_id="ag_a")
            api.action("ws_a", "history", agent_id="ag_a")
            api.status()
        self.assertEqual(seen, [
            ("POST", "/api/workspaces", {"name": "billing"}),
            ("PATCH", "/api/workspaces/ws_a", {"name": "tools"}),
            ("POST", "/api/workspaces/ws_a/agents", {"provider": "claude"}),
            ("POST", "/api/workspaces/ws_a/archive", None),
            ("POST", "/api/workspaces/ws_a/unarchive", None),
            ("POST", "/api/workspaces/ws_a/checkpoint", None),
            ("POST", "/api/workspaces/ws_a/agents/ag_a/stop", None),
            ("POST", "/api/workspaces/ws_a/agents/ag_a/retry", None),
            ("POST", "/api/workspaces/ws_a/agents/ag_a/resume", {}),
            ("POST", "/api/workspaces/ws_a/agents/ag_a/history", {}),
            ("GET", "/api/status", None),
        ])

    def test_resolve_fetches_archived_sessions_by_default(self):
        seen = []

        class Handler(_QuietHandler):
            def do_GET(self):
                seen.append(self.path)
                if self.path == "/":
                    data = b'<script>window.__SESSION_TOKEN__="token";</script>'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_json(200, {"workspaces": [
                        {"id": "ws_old", "name": "old", "archived": True, "agents": []}
                    ]})

        with serve(Handler) as url:
            self.assertEqual(WorkspaceAPI(url).resolve("old")["id"], "ws_old")
        self.assertIn("/api/workspaces?include_archived=1", seen)

    def test_mutation_auth_failure_is_not_retried_and_next_call_refreshes(self):
        state = {"token": "first", "bootstrap": 0, "requests": 0}

        class Handler(_QuietHandler):
            def authorized_reply(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                state["requests"] += 1
                if self.headers.get("X-Session-Token") != state["token"]:
                    self.send_json(401, {"error": "invalid token"})
                else:
                    self.send_json(200, {"workspaces": []})

            def do_GET(self):
                if self.path == "/":
                    state["bootstrap"] += 1
                    data = (f'<script>window.__SESSION_TOKEN__={json.dumps(state["token"])};'
                            f'</script>').encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.authorized_reply()

            do_POST = authorized_reply

        with serve(Handler) as url:
            api = WorkspaceAPI(url)
            api.list()
            state["token"] = "second"
            with self.assertRaises(CLIError) as caught:
                api.create("must-run-once")
            self.assertEqual(caught.exception.status, 401)
            self.assertEqual(state["requests"], 2)
            api.list()
        self.assertEqual(state, {"token": "second", "bootstrap": 2, "requests": 3})

    def test_single_deadline_bounds_bootstrap_and_request(self):
        class FakeResponse:
            def __init__(self, body, url):
                self.body = body
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self):
                return self.body

            def geturl(self):
                return self.url

        class FakeOpener:
            def __init__(self):
                self.timeouts = []

            def open(self, request, timeout):
                self.timeouts.append(timeout)
                url = request if isinstance(request, str) else request.full_url
                if len(self.timeouts) == 1:
                    clock[0] = 3.0
                    return FakeResponse(
                        b'<script>window.__SESSION_TOKEN__="token";</script>', url)
                return FakeResponse(b"{}", url)

        clock = [0.0]
        opener = FakeOpener()
        with patch("cli_api.monotonic", side_effect=lambda: clock[0]), \
                patch("cli_api.build_opener", return_value=opener):
            WorkspaceAPI("http://127.0.0.1:8300", timeout=5).status()
        self.assertEqual(opener.timeouts, [5, 2])

    def test_expired_deadline_does_not_start_json_request(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self):
                return b'<script>window.__SESSION_TOKEN__="token";</script>'

            def geturl(self):
                return "http://127.0.0.1:8300/"

        class FakeOpener:
            calls = 0

            def open(self, request, timeout):
                self.calls += 1
                clock[0] = 5.0
                return FakeResponse()

        clock = [0.0]
        opener = FakeOpener()
        with patch("cli_api.monotonic", side_effect=lambda: clock[0]), \
                patch("cli_api.build_opener", return_value=opener):
            with self.assertRaisesRegex(CLIError, "timed out"):
                WorkspaceAPI("http://127.0.0.1:8300", timeout=5).status()
        self.assertEqual(opener.calls, 1)

    def test_bootstrap_accepts_timeout_and_rejects_redirect(self):
        reached = []

        class Target(_QuietHandler):
            def do_GET(self):
                reached.append(self.path)
                data = b'<script>window.__SESSION_TOKEN__="stolen";</script>'
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with serve(Target) as target_url:
            class Redirect(_QuietHandler):
                def do_GET(self):
                    self.send_response(302)
                    self.send_header("Location", target_url + "/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            with serve(Redirect) as url:
                with self.assertRaisesRegex(ValueError, "redirect"):
                    fetch_session_token(url, timeout=0.5)
        self.assertEqual(reached, [])


if __name__ == "__main__":
    unittest.main()
