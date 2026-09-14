"""Loop guard settings from the TUI, including live server persistence."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
import tempfile
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from _cli_server import IsolatedCliServer
from cli_api import CLIError, request_json
from tests.test_cli_tui_workflows import workspace, workflow_harness


class LoopGuardPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_default_and_saved_update_reach_live_router_and_clients(self):
        import app
        from router import Router
        frames = []
        class Client:
            async def send_text(self, text): frames.append(json.loads(text))
        class Request:
            async def body(self): return b'{"max_agent_hops": 30}'
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            router = Router(['writer', 'reviewer'], max_hops=7)
            router._get_ch('work')['paused'] = True
            for name, value in {'router': router, 'room_settings': {'username': 'human'},
                                'config': {'server': {'data_dir': directory}},
                                'ws_clients': {Client()}}.items():
                stack.enter_context(patch.object(app, name, value))
            self.assertEqual((await app.get_settings())['max_agent_hops'], 7)
            self.assertEqual(await app.update_loop_guard(Request()), {'max_agent_hops': 30})
            self.assertEqual(router.max_hops, 30)
            self.assertTrue(router.is_paused('work'))
            self.assertEqual(frames[-1]['data']['max_agent_hops'], 30)
            saved = json.loads((Path(directory) / 'settings.json').read_text())
            self.assertEqual(saved, {'username': 'human', 'max_agent_hops': 30})
            app.room_settings.clear()
            app._load_settings()
            self.assertEqual(app.room_settings['max_agent_hops'], 30)

    async def test_failed_file_replace_preserves_previous_file_and_runtime_limit(self):
        import app
        from router import Router
        class Request:
            async def body(self): return b'{"max_agent_hops": 20}'
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            original = '{"username":"human","max_agent_hops":4}'
            path = Path(directory) / 'settings.json'
            path.write_text(original)
            for name, value in {'router': Router([], max_hops=4),
                                'room_settings': {'username': 'human', 'max_agent_hops': 4},
                                'config': {'server': {'data_dir': directory}}}.items():
                stack.enter_context(patch.object(app, name, value))
            with patch.object(Path, 'replace', side_effect=OSError('disk unavailable')):
                with self.assertLogs('app', level='ERROR'):
                    result = await app.update_loop_guard(Request())
            self.assertEqual(result.status_code, 500)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(app.router.max_hops, 4)
            self.assertEqual(app.room_settings['max_agent_hops'], 4)
            self.assertFalse(path.with_suffix('.tmp').exists())


class LoopGuardFormTests(unittest.IsolatedAsyncioTestCase):
    async def test_palette_edits_prefilled_limit_and_rejects_invalid_values(self):
        for plain in (False, True):
            async with workflow_harness(selected=None if plain else workspace(), plain=plain,
                                        size=(80, 24)) as ui:
                ui.api.settings.return_value = {'max_agent_hops': 12}
                ui.api.set_loop_guard.return_value = {'max_agent_hops': 20}
                ui.view.composer.text = 'keep draft'
                await ui.key('F4')
                await ui.type_text('Loop guard')
                await ui.key('Enter')
                await ui.wait_until(lambda: 'Maximum hops' in ui.screen_text())
                self.assertEqual(ui.application.current_buffer.text, '12')
                for bad in ('0', '51', 'abc', '2.5'):
                    await ui._send('\x01\x0b')
                    await ui.type_text(bad)
                    await ui.key('Enter')
                    await ui.wait_until(lambda: 'Enter a whole number from 1 to 50' in ui.screen_text())
                    ui.api.set_loop_guard.assert_not_called()
                await ui._send('\x01\x0b')
                await ui.type_text('20')
                await ui.key('Enter')
                await ui.wait_until(lambda: ui.api.set_loop_guard.call_count == 1)
                ui.api.set_loop_guard.assert_called_once_with(20)
                self.assertEqual(ui.view.composer.text, 'keep draft')

    async def test_cancel_and_failed_save_preserve_value_without_false_success(self):
        async with workflow_harness(selected=workspace()) as ui:
            ui.api.settings.return_value = {'max_agent_hops': 4}
            ui.api.set_loop_guard.side_effect = CLIError('Could not save settings', 500)
            task = ui.start(ui.workflows.run_action('loop_guard'))
            await ui.wait_until(lambda: 'Maximum hops' in ui.screen_text())
            await ui._send('\x01\x0b')
            await ui.type_text('25')
            await ui.key('Enter')
            await ui.wait_until(lambda: 'Could not save settings' in ui.screen_text())
            self.assertEqual(ui.application.current_buffer.text, '25')
            await ui.key('Escape')
            await ui.wait_until(task.done)
            self.assertEqual((await task).status, 'cancelled')
            self.assertEqual(ui.api.set_loop_guard.call_count, 1)


class LoopGuardServerTests(IsolatedCliServer):
    def test_authenticated_update_persists_and_invalid_requests_do_not_change_it(self):
        result = self.api.request('PATCH', '/api/settings/loop-guard', {'max_agent_hops': 20})
        self.assertEqual(result['max_agent_hops'], 20)
        self.assertEqual(self.api.request('GET', '/api/settings')['max_agent_hops'], 20)
        self.assertEqual(json.loads((self.data_dir / 'settings.json').read_text())['max_agent_hops'], 20)
        for bad in (0, 51, True, '20', 2.5, None):
            with self.assertRaises(CLIError) as caught:
                self.api.request('PATCH', '/api/settings/loop-guard', {'max_agent_hops': bad})
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(CLIError) as caught:
            request_json(self.url, 'wrong-token', 'PATCH', '/api/settings/loop-guard', {'max_agent_hops': 30})
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.api.request('GET', '/api/settings')['max_agent_hops'], 20)
