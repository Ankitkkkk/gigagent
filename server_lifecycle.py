"""Owned server shutdown and in-place restart. Never controls agent processes."""

import logging
import os
import secrets
import threading

log = logging.getLogger(__name__)
_RESTART_PARENT = '_AGENTCHATTR_RESTART_PARENT'


class RestartError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class ServerLifecycle:
    def __init__(self, argv, *, restart_supported=True, reason='', previous_instance_id=None):
        self.argv = list(argv)
        self.instance_id = secrets.token_hex(16)
        self.previous_instance_id = previous_instance_id
        self.restart_supported = restart_supported
        self.reason = reason
        self.restart_requested = False
        self.restart_environment = {}
        self.stop_event = threading.Event()
        self.servers = []
        self._workers = set()
        self._lock = threading.RLock()

    def add_server(self, server):
        self.servers.append(server)

    def status(self):
        with self._lock:
            ready = len(self.servers) == 3 and all(
                server.started and not server.should_exit for server in self.servers)
            return {
                'instance_id': self.instance_id,
                'previous_instance_id': self.previous_instance_id,
                'state': 'restarting' if self.restart_requested else 'ready' if ready else 'starting',
                'restart_supported': self.restart_supported,
                'reason': self.reason,
            }

    def request_restart(self, instance_id):
        """Reserve one restart. The HTTP response must precede begin_shutdown."""
        with self._lock:
            if not self.restart_supported:
                raise RestartError(self.reason, 503)
            if instance_id != self.instance_id:
                raise RestartError('Server changed. Reopen Restart server to confirm the current instance.')
            if not self.restart_requested and self.status()['state'] != 'ready':
                raise RestartError('Server is still starting. Check web and MCP listeners before retrying.')
            self.restart_requested = True
            return self.status()

    def start_worker(self, function, *args):
        """Track periodic and launcher workers; refuse new work during shutdown."""
        def run():
            try:
                function(*args)
            finally:
                with self._lock:
                    self._workers.discard(threading.current_thread())
        with self._lock:
            if self.stop_event.is_set():
                return False
            thread = threading.Thread(target=run, daemon=True)
            self._workers.add(thread)
            try:
                thread.start()
            except BaseException:
                self._workers.discard(thread)
                raise
            return True

    def begin_shutdown(self):
        with self._lock:
            self.stop_event.set()
            for server in self.servers:
                server.should_exit = True

    def drain(self):
        """Never exec while an owned worker might still be writing saved state.

        The client has a bounded readiness wait. A slow server worker may take
        longer; leave it draining instead of interrupting a persistence write.
        """
        while True:
            with self._lock:
                workers = tuple(self._workers)
            if not workers:
                return
            for worker in workers:
                worker.join(5)
                if worker.is_alive():
                    log.info('Waiting for server work to finish before shutdown')

    def replace_process(self):
        if self.restart_requested:
            log.info('Restarting agentchattr in place')
            overrides = dict(self.restart_environment, **{_RESTART_PARENT: self.instance_id})
            previous = {key: os.environ.get(key) for key in overrides}
            os.environ.update(overrides)
            try:
                os.execv(self.argv[0], self.argv)
            finally:
                # Only reached on exec failure (or a test double).
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
