"""Run the real shell installer with offline Python/download boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Each interpreter runs the installer's actual version-check expression. Only
# environment creation and pip are simulated, so tests never download packages
# or replace the developer's Python, commands, or installed yapp environment.
PYTHON_STUB = r'''
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys

version = __VERSION__
executable = Path(sys.argv[0])
args = sys.argv[1:]
if args[:2] == ['-m', 'venv']:
    target = Path(args[2])
    (target / 'bin').mkdir(parents=True)
    python = target / 'bin' / 'python'
    python.write_text(executable.read_text())
    python.chmod(0o755)
    (target / 'selected-python.json').write_text(json.dumps(version))
    (target / 'selected-source').write_text(str(executable))
elif args[:2] == ['-m', 'pip']:
    prefix = executable.parent.parent
    info = prefix / 'yapp-0.5.0.dist-info'
    info.mkdir(exist_ok=True)
    (info / 'METADATA').write_text('Name: yapp\nVersion: 0.5.0\n')
    for name in ('yapp', 'goon'):
        (prefix / 'bin' / name).write_text('#!/bin/sh\nexit 0\n')
    (prefix / 'pip-used').touch()
elif args and args[0] in ('-c', '-'):
    code = args[1] if args[0] == '-c' else sys.stdin.read()
    sys.argv = args if args[0] == '-' else ['-c', *args[2:]]
    sys.path.insert(0, str(executable.parent.parent))
    sys.version_info = (*version, 0, 'final', 0)
    platform.python_version = lambda: '.'.join(map(str, version)) + '.0'
    exec(code, {'__name__': '__main__'})
else:
    raise SystemExit('Unexpected interpreter invocation: ' + repr(args))
'''

UV_STUB = r'''
import os
from pathlib import Path
import shutil
import sys

if '--help' in sys.argv:
    raise SystemExit(0)
if os.environ.get('TEST_UV_FAIL'):
    raise SystemExit('Python download failed (offline test)')
root = Path(os.environ['UV_PYTHON_INSTALL_DIR'])
assert root.is_relative_to(Path(os.environ['YAPP_HOME']))
python = root / 'managed' / 'bin' / 'python'
if 'install' in sys.argv:
    assert '--no-bin' in sys.argv
    python.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(os.environ['TEST_MANAGED_PYTHON'], python)
elif 'find' in sys.argv:
    assert python.is_file()
    if '--system' not in sys.argv and os.environ.get('VIRTUAL_ENV'):
        python = Path(os.environ['VIRTUAL_ENV']) / 'bin' / 'python'
    print(python)
else:
    raise SystemExit('Unexpected uv invocation: ' + repr(sys.argv))
'''


@unittest.skipUnless(os.name == 'posix', 'installer requires a Unix shell')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='yapp-installer-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / 'commands'
        self.bin.mkdir()
        self.install = self.root / 'install with spaces'
        self.links = self.root / 'links'
        self.venv = self.install / 'venv'
        # No system PATH: a host Python/uv must not mask missing-interpreter tests.
        for name in ('sh', 'env', 'mkdir', 'rm', 'ln', 'readlink', 'basename',
                     'mktemp', 'cp', 'chmod'):
            source = shutil.which(name)
            if source:
                (self.bin / name).symlink_to(source)
        self.env = {
            'PATH': str(self.bin), 'HOME': str(self.root),
            'YAPP_HOME': str(self.install), 'YAPP_BIN_DIR': str(self.links),
            'YAPP_SOURCE': str(self.root / 'offline-yapp'),
            'TEST_MANAGED_PYTHON': str(self.root / 'managed-template'),
            'TEST_UV_SOURCE': str(self.root / 'uv-template'),
        }
        self.python(self.root / 'managed-template', (3, 13))
        self.script(self.root / 'uv-template', UV_STUB)
        self.script(self.bin / 'curl', r'''
import os
from pathlib import Path
import sys
if os.environ.get('TEST_CURL_FAIL'):
    raise SystemExit('Installer download failed (offline test)')
target = Path(sys.argv[sys.argv.index('-o') + 1])
target.write_text('#!/bin/sh\nset -eu\n'
                  ': "${UV_UNMANAGED_INSTALL:?}"\n'
                  'mkdir -p "$UV_UNMANAGED_INSTALL"\n'
                  'cp "$TEST_UV_SOURCE" "$UV_UNMANAGED_INSTALL/uv"\n')
''')

    def script(self, path, code):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!{sys.executable}\n' + textwrap.dedent(code).lstrip())
        path.chmod(0o755)

    def python(self, path, version):
        self.script(path, PYTHON_STUB.replace('__VERSION__', repr(version)))

    def run_installer(self, *, streamed=False, **env):
        return subprocess.run(['/bin/sh'] + ([] if streamed else [str(ROOT / 'install.sh')]),
                              env={**self.env, **env}, capture_output=True,
                              text=True, timeout=20,
                              input=(ROOT / 'install.sh').read_text() if streamed else None)

    def assert_installed(self, result, version):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.venv / 'selected-python.json').read_text()), list(version))
        for name in ('yapp', 'goon'):
            self.assertEqual((self.links / name).resolve(), self.venv / 'bin' / name)
        marker = json.loads((self.venv / 'yapp-install.json').read_text())
        self.assertEqual(marker['method'], 'installer')
        self.assertEqual(marker['source'], self.env['YAPP_SOURCE'])

    def test_python_310_system_gets_private_supported_runtime(self):
        system = self.bin / 'python3'
        self.python(system, (3, 10))
        original = system.read_bytes()
        self.assert_installed(self.run_installer(), (3, 13))
        self.assertEqual(system.read_bytes(), original)
        self.assertFalse((self.bin / 'python3.13').exists())

    def test_install_without_any_system_python(self):
        self.assert_installed(self.run_installer(), (3, 13))

    def test_streamed_install_can_bootstrap_python(self):
        self.assert_installed(self.run_installer(streamed=True), (3, 13))

    def test_supported_python_versions_do_not_download_runtime(self):
        for version in ((3, 11), (3, 12), (3, 13), (3, 14)):
            with self.subTest(version=version):
                self.python(self.bin / 'python3', version)
                self.assert_installed(self.run_installer(TEST_CURL_FAIL='1'), version)
                shutil.rmtree(self.venv)

    def test_versioned_interpreter_used_when_python3_is_old(self):
        self.python(self.bin / 'python3', (3, 10))
        self.python(self.bin / 'python3.12', (3, 12))
        self.assert_installed(self.run_installer(TEST_CURL_FAIL='1'), (3, 12))

    def test_explicit_python_is_respected(self):
        self.python(self.bin / 'python3', (3, 14))
        chosen = self.root / 'chosen python'
        self.python(chosen, (3, 12))
        self.assert_installed(self.run_installer(PYTHON=str(chosen), TEST_CURL_FAIL='1'), (3, 12))

    def test_explicit_unsupported_python_fails_before_mutating_install(self):
        self.python(self.bin / 'python3', (3, 10))
        result = self.run_installer(PYTHON='python3')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('3.11', result.stderr)
        self.assertFalse(self.venv.exists())

    def test_existing_supported_venv_reused_with_old_system_python(self):
        self.python(self.bin / 'python3', (3, 10))
        self.python(self.venv / 'bin' / 'python', (3, 12))
        (self.venv / 'selected-python.json').write_text('[3, 12]')
        self.assert_installed(self.run_installer(TEST_CURL_FAIL='1'), (3, 12))

    def test_existing_unsupported_venv_is_preserved(self):
        self.python(self.bin / 'python3', (3, 12))
        self.python(self.venv / 'bin' / 'python', (3, 10))
        sentinel = self.venv / 'user-file'
        sentinel.write_text('keep')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertFalse((self.venv / 'pip-used').exists())

    def test_incomplete_existing_venv_is_preserved(self):
        self.python(self.bin / 'python3', (3, 12))
        self.venv.mkdir(parents=True)
        sentinel = self.venv / 'user-file'
        sentinel.write_text('keep')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertFalse((self.venv / 'pip-used').exists())

    def test_existing_uv_used_without_bootstrap_download(self):
        self.script(self.bin / 'uv', UV_STUB)
        self.assert_installed(self.run_installer(TEST_CURL_FAIL='1'), (3, 13))

    def test_old_uv_replaced_privately_without_changing_original(self):
        for path in (self.bin / 'uv', self.install / 'tools' / 'uv'):
            self.script(path, "raise SystemExit('unknown option: --no-bin')")
        original = (self.bin / 'uv').read_bytes()
        self.assert_installed(self.run_installer(), (3, 13))
        self.assertEqual((self.bin / 'uv').read_bytes(), original)

    def test_managed_runtime_selected_over_active_virtual_environment(self):
        external = self.root / 'external-venv'
        self.python(external / 'bin' / 'python', (3, 13))
        self.assert_installed(self.run_installer(VIRTUAL_ENV=str(external)), (3, 13))
        selected = Path((self.venv / 'selected-source').read_text())
        self.assertTrue(selected.is_relative_to(self.install / 'python'), selected)

    def test_failed_runtime_download_does_not_create_venv(self):
        result = self.run_installer(TEST_UV_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Python download failed', result.stderr)
        self.assertFalse(self.venv.exists())

    def test_failed_bootstrap_download_does_not_create_venv(self):
        result = self.run_installer(TEST_CURL_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Installer download failed', result.stderr)
        self.assertFalse(self.venv.exists())


if __name__ == '__main__':
    unittest.main()
