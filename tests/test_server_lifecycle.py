"""Restart ownership without touching a real process or server."""
import threading
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server_lifecycle import ServerLifecycle, RestartError


class ServerLifecycleTests(unittest.TestCase):
    def lifecycle(self, **kwargs):
        lifecycle = ServerLifecycle(['/python', '-u', '/repo/run.py', '--port', '18300'], **kwargs)
        for _ in range(3):
            lifecycle.add_server(SimpleNamespace(started=True, should_exit=False))
        return lifecycle

    def test_new_identity_and_all_transports_required_for_ready(self):
        lifecycle = self.lifecycle()
        other = self.lifecycle()
        self.assertNotEqual(lifecycle.instance_id, other.instance_id)
        self.assertEqual(lifecycle.status()['state'], 'ready')
        lifecycle.servers[1].started = False
        self.assertEqual(lifecycle.status()['state'], 'starting')
        with self.assertRaises(RestartError):
            lifecycle.request_restart(lifecycle.instance_id)
        self.assertFalse(lifecycle.restart_requested)

    def test_stale_request_does_not_stop_anything_and_duplicates_coalesce(self):
        lifecycle = self.lifecycle()
        with self.assertRaises(RestartError) as error:
            lifecycle.request_restart('old-boot')
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(lifecycle.stop_event.is_set())
        first = lifecycle.request_restart(lifecycle.instance_id)
        self.assertEqual(first, lifecycle.request_restart(lifecycle.instance_id))
        self.assertTrue(lifecycle.restart_requested)
        self.assertFalse(lifecycle.stop_event.is_set())  # Response comes first.
        lifecycle.begin_shutdown()
        lifecycle.begin_shutdown()
        self.assertTrue(lifecycle.stop_event.is_set())
        self.assertTrue(all(s.should_exit for s in lifecycle.servers))

    def test_unsupported_owner_refuses_without_shutdown(self):
        lifecycle = self.lifecycle(restart_supported=False, reason='Use run.py on localhost')
        with self.assertRaises(RestartError) as error:
            lifecycle.request_restart(lifecycle.instance_id)
        self.assertEqual(error.exception.status, 503)
        self.assertFalse(lifecycle.restart_requested)

    def test_shutdown_drains_owned_worker_before_exec_and_rejects_new_workers(self):
        lifecycle = self.lifecycle()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def worker():
            entered.set()
            release.wait(5)
            finished.set()
        lifecycle.start_worker(worker)
        self.assertTrue(entered.wait(2))
        lifecycle.request_restart(lifecycle.instance_id)
        lifecycle.begin_shutdown()
        self.assertFalse(lifecycle.start_worker(lambda: self.fail('new worker admitted')))
        joiner = threading.Thread(target=lifecycle.drain)
        joiner.start()
        try:
            joiner.join(.02)
            self.assertTrue(joiner.is_alive())
            release.set()
            joiner.join(2)
            self.assertFalse(joiner.is_alive())
            self.assertTrue(finished.is_set())
            with patch('server_lifecycle.os.execv') as execute:
                lifecycle.replace_process()
            execute.assert_called_once_with('/python', ['/python', '-u', '/repo/run.py', '--port', '18300'])
        finally:
            release.set()
            joiner.join(2)

    def test_normal_shutdown_never_executes(self):
        lifecycle = self.lifecycle()
        lifecycle.begin_shutdown()
        lifecycle.drain()
        with patch('server_lifecycle.os.execv') as execute:
            lifecycle.replace_process()
        execute.assert_not_called()

    def test_exec_failure_is_reported_without_launching_fallback_process(self):
        lifecycle = self.lifecycle()
        lifecycle.request_restart(lifecycle.instance_id)
        lifecycle.begin_shutdown()
        lifecycle.drain()
        with patch('server_lifecycle.os.execv', side_effect=OSError('missing executable')):
            with self.assertRaises(OSError):
                lifecycle.replace_process()

    def test_exec_provides_successor_marker_and_restores_environment_on_failure(self):
        lifecycle = self.lifecycle(previous_instance_id='previous-boot')
        self.assertEqual(lifecycle.status()['previous_instance_id'], 'previous-boot')
        lifecycle.request_restart(lifecycle.instance_id)
        lifecycle.begin_shutdown()
        lifecycle.drain()
        lifecycle.restart_environment = {'YAPP_PORT': '19300'}
        def replace(*args):
            self.assertEqual(os.environ['YAPP_PORT'], '19300')
            self.assertEqual(os.environ['RESTART_TEST_UNRELATED'], 'kept')
            self.assertEqual(os.environ['_YAPP_RESTART_PARENT'], lifecycle.instance_id)
            raise OSError('test failure')
        with patch.dict(os.environ, {'_YAPP_RESTART_PARENT': 'original',
                                   'YAPP_PORT': '18300', 'RESTART_TEST_UNRELATED': 'kept'}), \
                patch('server_lifecycle.os.execv', side_effect=replace):
            with self.assertRaises(OSError):
                lifecycle.replace_process()
            self.assertEqual(os.environ['_YAPP_RESTART_PARENT'], 'original')
            self.assertEqual(os.environ['YAPP_PORT'], '18300')

    def test_uvicorn_drains_accepted_executor_write_after_graceful_timeout(self):
        import asyncio
        import socket
        from urllib.request import urlopen
        from urllib.error import HTTPError
        import uvicorn
        from fastapi import FastAPI

        entered, release, finished, cancelled = (threading.Event() for _ in range(4))
        lifecycle = self.lifecycle()
        app = FastAPI()

        def write():
            entered.set()
            release.wait(15)
            finished.set()

        @app.get('/write')
        async def endpoint():
            try:
                await asyncio.to_thread(write)
            finally:
                cancelled.set()
            return {'saved': True}

        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.listen()
        self.addCleanup(sock.close)
        server = uvicorn.Server(uvicorn.Config(app, log_level='critical', loop='asyncio',
                                               timeout_graceful_shutdown=5))
        lifecycle.servers[-1] = server
        lifecycle.start_worker(lambda: server.run(sockets=[sock]))
        caller = None
        joiner = None
        def request():
            try:
                with urlopen(f'http://127.0.0.1:{port}/write', timeout=15) as response:
                    response.read()
            except (OSError, HTTPError):
                pass
        try:
            caller = threading.Thread(target=request)
            caller.start()
            self.assertTrue(entered.wait(5), 'real HTTP handler never entered its executor write')
            lifecycle.request_restart(lifecycle.instance_id)
            lifecycle.begin_shutdown()
            self.assertTrue(cancelled.wait(8), 'handler was not cancelled by graceful timeout')
            self.assertFalse(finished.is_set())
            joiner = threading.Thread(target=lifecycle.drain)
            joiner.start()
            joiner.join(.05)
            self.assertTrue(joiner.is_alive(), 'server runner abandoned a still-writing executor')
            release.set()
            joiner.join(3)
            self.assertFalse(joiner.is_alive())
            self.assertTrue(finished.is_set())
        finally:
            release.set()
            lifecycle.begin_shutdown()
            lifecycle.drain()
            if caller:
                caller.join(3)
            if joiner:
                joiner.join(3)
