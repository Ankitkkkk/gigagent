"""HTTP forms reject profile edits and retain failed session creation outcomes."""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
import app as application
import mcp_bridge
from tests import test_workspace_launcher as fixtures


class OrchestrationApiTests(unittest.TestCase):
    setUp = fixtures.LauncherTests.setUp

    def exercise(self, function):
        isolated = FastAPI(routes=application.app.routes,
                           exception_handlers=application.app.exception_handlers)
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated),
                                         base_url='http://localhost',
                                         headers={'X-Session-Token':'profile-test'}) as client:
                await function(client)
        with patch.object(application, 'app', isolated), \
                patch.object(application, 'session_token', 'profile-test'), \
                patch.object(application, 'workspace_launcher', self.launcher), \
                patch.object(application, 'workspace_store', self.store), \
                patch.object(application, '_ensure_channel'), \
                patch.object(application, 'broadcast_settings'), \
                patch.object(mcp_bridge, 'saved_profile', application._saved_profile), \
                patch.object(mcp_bridge, 'saved_profile_names', self.store.member_names):
            application._install_security_middleware('profile-test', {})
            asyncio.run(request())

    def test_new_session_provider_and_frozen_role_http_contract(self):
        async def check(client):
            response = await client.post('/api/workspaces', json={'name':'routed', 'orchestrator':
                {'provider':'kilo', 'cwd':str(self.proj)}})
            self.assertEqual(response.status_code, 200, response.text)
            ws = response.json()
            self.assertEqual(len(ws['agents']), 1)
            self.assertEqual(ws['agents'][0]['kind'], 'orchestrator')
            self.assertTrue(ws['orchestrator']['enabled'])
            response = await client.post(f"/api/workspaces/{ws['id']}/agents", json={
                'provider':'claude', 'cwd':str(self.proj), 'history_mode':'none',
                'role':'code-reviewer', 'personality':'meticulous'})
            self.assertEqual(response.status_code, 200, response.text)
            agent = response.json()
            self.assertEqual(agent['profile']['role'], 'code-reviewer')
            self.assertEqual(agent['profile']['personality'], 'meticulous')
            roles = (await client.get('/api/roles')).json()
            self.assertEqual(roles[agent['registry_name']], 'code-reviewer')
            edit = await client.post('/api/roles/' + agent['registry_name'], json={'role':'planner'})
            self.assertEqual(edit.status_code, 409, edit.text)
            resume = await client.post(f"/api/workspaces/{ws['id']}/agents/{agent['agent_id']}/resume",
                                       json={'fresh':True, 'role':'planner'})
            self.assertEqual(resume.status_code, 409, resume.text)
            self.assertEqual(self.store.get_agent(ws['id'], agent['agent_id'])['profile'], agent['profile'])
        self.exercise(check)

    def test_invalid_orchestrator_creates_nothing_and_legacy_create_launches_nothing(self):
        async def check(client):
            before = len(self.store.list())
            for manager in ({}, {'provider':'missing', 'cwd':str(self.proj)},
                            {'provider':'kilo', 'cwd':'relative'},
                            {'provider':'kilo', 'cwd':str(self.proj), 'provider_args':'bad'}):
                response = await client.post('/api/workspaces', json={'name':'bad', 'orchestrator':manager})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(len(self.store.list()), before)
            response = await client.post('/api/workspaces', json={'name':'legacy'})
            self.assertEqual(response.json()['agents'], [])
            self.assertFalse(self.popen_calls)
        self.exercise(check)

    def test_launch_failure_returns_saved_session_for_deliberate_retry(self):
        async def check(client):
            with patch.object(self.launcher, '_popen', side_effect=OSError('cannot launch')):
                response = await client.post('/api/workspaces', json={'name':'retry-session', 'orchestrator':
                    {'provider':'kilo', 'cwd':str(self.proj)}})
            self.assertEqual(response.status_code, 200, response.text)
            ws = response.json()
            self.assertIn('cannot launch', ws['orchestration_error'])
            self.assertIsNotNone(self.store.get(ws['id']))
            self.assertFalse(ws['orchestrator']['enabled'])
            again = await client.post(f"/api/workspaces/{ws['id']}/orchestrator", json={
                'provider':'kilo', 'cwd':str(self.proj)})
            self.assertEqual(again.status_code, 200, again.text)
            self.assertEqual(len(again.json()['agents']), 1)
        self.exercise(check)

    def test_archive_pauses_supervision_before_cleanup_and_keeps_saved_identity(self):
        async def check(client):
            ws = (await client.post('/api/workspaces', json={'name':'archive-race', 'orchestrator':
                {'provider':'kilo', 'cwd':str(self.proj)}})).json()
            manager = ws['agents'][0]
            self.store.update_agent(ws['id'], manager['agent_id'], last_state='exited')
            self.launcher._processes[manager['agent_id']].returncode = 1
            worker = self.launcher.spawn(ws['id'], 'claude', str(self.proj), 'none')
            self.store.update_agent(ws['id'], worker['agent_id'], last_state='running')
            before = len(self.popen_calls)
            with patch.object(self.launcher, 'checkpoint', side_effect=lambda *a, **k: self.launcher.tick()):
                response = await client.post(f"/api/workspaces/{ws['id']}/archive")
            self.assertEqual(response.status_code, 200, response.text)
            final = response.json()
            self.assertTrue(final['archived'])
            self.assertFalse(final['orchestrator']['enabled'])
            self.assertTrue(all(agent['last_state'] == 'exited' for agent in final['agents']))
            self.assertEqual(len(self.popen_calls), before)
            self.assertTrue(self.store.identity_path(manager['agent_id']).exists())
        self.exercise(check)
