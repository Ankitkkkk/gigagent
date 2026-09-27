"""Human-only, instance-fenced restart API; all process actions faked."""
import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
import app as application
from server_lifecycle import ServerLifecycle


class ServerRestartApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lifecycle = ServerLifecycle(['/python', '/repo/run.py'])
        for _ in range(3):
            self.lifecycle.add_server(SimpleNamespace(started=True, should_exit=False))
        isolated = FastAPI(routes=application.app.routes)
        for attr, value in [('app', isolated), ('server_lifecycle', self.lifecycle),
                            ('session_token', 'restart-test')]:
            patcher = patch.object(application, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        application._install_security_middleware('restart-test', {})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated),
                                       base_url='http://127.0.0.1:8300')
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {'X-Session-Token': 'restart-test'}

    async def test_missing_agent_only_and_foreign_origin_rejected(self):
        for headers in ({}, {'Authorization': 'Bearer fake-agent'},
                        {**self.headers, 'Origin': 'http://evil.invalid'}):
            response = await self.client.post('/api/server/restart', headers=headers,
                                json={'instance_id': self.lifecycle.instance_id})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.lifecycle.restart_requested)

    async def test_valid_restart_and_duplicate_returns_accepted_same_instance(self):
        status = await self.client.get('/api/server', headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['state'], 'ready')
        for _ in range(2):
            response = await self.client.post('/api/server/restart', headers=self.headers,
                                             json={'instance_id': self.lifecycle.instance_id})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()['instance_id'], self.lifecycle.instance_id)
            self.assertEqual(response.json()['state'], 'restarting')
        self.assertTrue(self.lifecycle.stop_event.is_set())

    async def test_stale_or_invalid_body_never_restarts(self):
        for body in ({}, {'instance_id': ''}, {'instance_id': 7},
                     {'instance_id': self.lifecycle.instance_id, 'pid': 123},
                     {'instance_id': 'old-instance'}):
            response = await self.client.post('/api/server/restart', headers=self.headers, json=body)
            self.assertIn(response.status_code, (400, 409))
        self.assertFalse(self.lifecycle.restart_requested)

    async def test_unowned_entrypoint_reports_manual_start_and_refuses(self):
        with patch.object(application, 'server_lifecycle', None):
            status = await self.client.get('/api/server', headers=self.headers)
            self.assertFalse(status.json()['restart_supported'])
            response = await self.client.post('/api/server/restart', headers=self.headers,
                                             json={'instance_id': 'unowned'})
            self.assertEqual(response.status_code, 503)
        self.assertFalse(self.lifecycle.restart_requested)

    async def test_accepted_restart_still_starts_when_response_send_fails(self):
        from starlette.requests import Request
        async def receive():
            return {'type': 'http.request', 'body': json.dumps({
                'instance_id': self.lifecycle.instance_id}).encode()}
        async def send(message):
            if message['type'] == 'http.response.body':
                raise OSError('client disconnected before acknowledgement')
        scope = {'type': 'http', 'method': 'POST', 'path': '/api/server/restart', 'headers': []}
        response = await application.restart_server(Request(scope, receive))
        self.assertTrue(self.lifecycle.restart_requested)
        self.assertFalse(self.lifecycle.stop_event.is_set())
        with self.assertRaises(OSError):
            await response(scope, receive, send)
        self.assertTrue(self.lifecycle.stop_event.is_set())
        self.assertTrue(all(server.should_exit for server in self.lifecycle.servers))
