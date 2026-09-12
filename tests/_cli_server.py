"""Setup-only real-server fixture; never starts a provider CLI by itself."""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cli
from cli_workspaces import WorkspaceAPI


def temporary_ports():
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def isolated_environment(directory, additions=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('AGENTCHATTR_') and k not in ('TMUX', 'TMUX_TMPDIR')}
    socket_dir = Path(directory) / 'tmux'
    socket_dir.mkdir(exist_ok=True)
    env['TMUX_TMPDIR'] = str(socket_dir)
    env.update(additions or {})
    return env


def log_excerpt(path):
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return '(server log unavailable)'
    text = re.sub(r'([?&]token=)[^\s&\"\'<>]+', r'\1[REDACTED]', text,
                  flags=re.IGNORECASE)
    text = re.sub(r'(Session token:\s*)\S+', r'\1[REDACTED]', text,
                  flags=re.IGNORECASE)
    return text[-12000:]


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    else:
        process.wait()


class IsolatedCliServer(unittest.TestCase):
    """Shared setup only: subclasses define every test exactly once."""

    @classmethod
    def environment_additions(cls):
        return {}

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='agentchattr-cli-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.log_path = Path(cls.temp.name) / 'server.log'
        cls.log = cls.log_path.open('w+')
        cls.addClassCleanup(cls.log.close)
        cls.ports = temporary_ports()
        cls.url = f'http://127.0.0.1:{cls.ports[0]}'
        cls.env = isolated_environment(cls.temp.name, cls.environment_additions())
        cls.data_dir = Path(cls.temp.name) / 'data'
        cls.upload_dir = Path(cls.temp.name) / 'uploads'
        cls.process = subprocess.Popen([
            sys.executable, 'run.py', '--port', str(cls.ports[0]),
            '--mcp-http-port', str(cls.ports[1]), '--mcp-sse-port', str(cls.ports[2]),
            '--data-dir', str(cls.data_dir), '--upload-dir', str(cls.upload_dir),
        ], cwd=ROOT, stdout=cls.log, stderr=cls.log, env=cls.env)
        cls.addClassCleanup(stop_process, cls.process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                break
            try:
                cls.token = cli.fetch_session_token(cls.url)
                cls.api = WorkspaceAPI(cls.url)
                cls.api.status()
                return
            except (OSError, cli.CLIError):
                time.sleep(.05)
        raise RuntimeError('Isolated server failed readiness:\n' + log_excerpt(cls.log_path))

    def tearDown(self):
        result = self._outcome.result
        if any(test is self or getattr(test, 'test_case', None) is self
               for test, _ in result.failures + result.errors):
            sys.stderr.write('\nIsolated server log:\n' + log_excerpt(self.log_path) + '\n')

    def command(self, *args, input=None):
        return subprocess.run([sys.executable, 'cli.py', '--url', self.url, *args],
                              cwd=ROOT, text=True, capture_output=True, input=input,
                              timeout=25, env=self.env)

    def json_command(self, *args):
        result = self.command(*args, '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def poll(self, predicate, *, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.05)
        self.fail('Condition did not become true before deadline')
