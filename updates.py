"""Release checks and self-update for installed copies of yapp.

Standard library only, so it works in every install type.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = 'Ankitkkkk/yapp'
API_URL = f'https://api.github.com/repos/{REPO}/releases/latest'
INSTALLER_COMMAND = 'curl -fsSL https://yapp.riggedcode.com/install.sh | sh'
CHECK_TTL = 6 * 3600
FAILURE_TTL = 3600
CHECK_TIMEOUT = 5
APPLY_TIMEOUT = 600
PROBE_TIMEOUT = 60
# Two install commands plus the probe; an update lock younger than this is still live.
LOCK_STALE_AFTER = 2 * APPLY_TIMEOUT + 120
RELAUNCH_MAX_AGE = 600
CACHE_FILE = 'update_check.json'
LOCK_FILE = 'update.lock'
INCOMPLETE_FILE = 'update.incomplete'
RELAUNCH_FILE = 'relaunch_state.{pid}.json'
RELAUNCH_ENV = 'YAPP_RELAUNCH_STATE'
_RELAUNCH_NAME = re.compile(r'^relaunch_state\.\d+\.json$')
MARKER = 'yapp-install.json'
_VERSION = re.compile(r'^v?(\d+(?:\.\d+)*)$')


def parse_version(text):
    match = _VERSION.match(str(text or '').strip())
    return tuple(int(part) for part in match.group(1).split('.')) if match else None


def compare(current, latest):
    """Return 'update_available', 'current', or 'unknown'; never guess."""
    a, b = parse_version(current), parse_version(latest)
    if a is None or b is None:
        return 'unknown'
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    return 'update_available' if b > a else 'current'


def installed_version(root=ROOT):
    """VERSION as it is on disk now; differs from RUNNING_VERSION after an update."""
    try:
        return (Path(root) / 'VERSION').read_text().strip()
    except OSError:
        return ''


RUNNING_VERSION = installed_version()


def _flag(environ, name):
    return str(environ.get(name, '')).strip().lower() in ('1', 'true', 'yes', 'on')


def check_enabled(config, environ=os.environ):
    settings = (config or {}).get('updates', {})
    return not _flag(environ, 'YAPP_NO_UPDATE_CHECK') and settings.get('check', True) is not False


def auto_enabled(config, environ=os.environ):
    settings = (config or {}).get('updates', {})
    return (check_enabled(config, environ) and not _flag(environ, 'YAPP_NO_AUTO_UPDATE')
            and settings.get('auto', True) is not False)


def data_dir_from_config(config):
    path = Path((config or {}).get('server', {}).get('data_dir', './data'))
    return path if path.is_absolute() else ROOT / path


def latest_release(*, opener=urllib.request.urlopen, environ=os.environ):
    url = environ.get('YAPP_UPDATE_URL') or API_URL
    request = urllib.request.Request(
        url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'yapp'})
    with opener(request, timeout=CHECK_TIMEOUT) as response:
        data = json.loads(response.read())
    tag = data.get('tag_name') if isinstance(data, dict) else None
    if not isinstance(tag, str) or parse_version(tag) is None:
        raise ValueError('release has no version tag')
    return {'tag': tag, 'latest': tag.lstrip('v'),
            'url': str(data.get('html_url') or f'https://github.com/{REPO}/releases/tag/{tag}'),
            'archive_url': f'https://github.com/{REPO}/archive/refs/tags/{tag}.zip'}


def _reason(error):
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (403, 429):
            return 'GitHub rate limit reached; try again later.'
        if error.code == 404:
            return 'No yapp release has been published yet.'
        return f'GitHub returned HTTP {error.code}.'
    if isinstance(error, (urllib.error.URLError, OSError, TimeoutError)):
        return 'Could not reach GitHub to check for updates.'
    return 'Unexpected response from GitHub.'


def _read_cache(path, now):
    try:
        record = json.loads(path.read_text())
        age = now - float(record['checked_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ttl = FAILURE_TTL if record.get('error') else CHECK_TTL
    return record if 0 <= age < ttl else None


def _write_cache(path, record):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    except OSError:
        pass


def check(*, data_dir, config=None, force=False, explicit=False, current=None,
          opener=urllib.request.urlopen, environ=os.environ, now=time.time):
    current = RUNNING_VERSION if current is None else current
    result = {'state': 'unknown', 'current': current, 'latest': '', 'tag': '',
              'url': '', 'archive_url': '', 'error': ''}
    if not explicit and not check_enabled(config, environ):
        return dict(result, state='disabled')
    cache = Path(data_dir) / CACHE_FILE
    record = None if force else _read_cache(cache, now())
    if record is None:
        try:
            record = dict(latest_release(opener=opener, environ=environ), error='')
        except Exception as error:  # Network, HTTP, JSON, or tag errors all mean "unknown".
            record = {'error': _reason(error)}
        record['checked_at'] = now()
        _write_cache(cache, record)
    if record.get('error'):
        return dict(result, error=record['error'])
    for key in ('latest', 'tag', 'url', 'archive_url'):
        result[key] = str(record.get(key, ''))
    result['state'] = compare(current, result['latest'])
    return result


_VERSION_PROBE = 'from importlib.metadata import version; print(version("yapp"))'


def install_method(*, prefix=sys.prefix, root=ROOT, which=shutil.which):
    if (Path(root) / 'pyproject.toml').exists():
        return 'checkout'
    prefix = Path(prefix)
    if (prefix / MARKER).is_file():
        return 'installer'
    if prefix.resolve().parts[-3:] == ('pipx', 'venvs', 'yapp') and which('pipx'):
        return 'pipx'
    return 'unknown'


def manual_instructions(method):
    if method == 'checkout':
        return 'This is a source checkout: run git pull, then restart yapp.'
    if method == 'unknown':
        return f'Update with: {INSTALLER_COMMAND}'
    return ''


def _acquire_lock(path, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if now() - path.stat().st_mtime > LOCK_STALE_AFTER:
                    path.unlink()  # A crashed update left it behind.
                    continue
            except OSError:
                pass
            return None
        with os.fdopen(fd, 'w') as handle:
            handle.write(str(os.getpid()))
        return path
    return None


def _tail(text, lines=3):
    return ' '.join(line.strip() for line in str(text or '').strip().splitlines()[-lines:])


def apply(release, *, method, data_dir, python=sys.executable, runner=subprocess.run,
          which=shutil.which, environ=os.environ, now=time.time):
    """Install `release` for this install method. Never raises for expected failures."""
    if method not in ('installer', 'pipx'):
        return {'ok': False, 'state': 'unsupported', 'version': '',
                'message': manual_instructions(method)}
    archive = environ.get('YAPP_UPDATE_ARCHIVE') or release['archive_url']
    # Upgrade in place, then force this package's files over whatever was there.
    # For pipx, `pipx install --force` could delete the venv on failure and would
    # rewrite pipx's recorded package spec, so pip runs inside the existing venv.
    if method == 'installer':
        pip = [python, '-m', 'pip']
        manual = f'Update manually with: {INSTALLER_COMMAND}'
    else:
        pip = [which('pipx') or 'pipx', 'runpip', 'yapp']
        manual = f'Update manually with: pipx runpip yapp install --upgrade {archive}'
    commands = [pip + ['install', '-q', '--upgrade', archive],
                pip + ['install', '-q', '--force-reinstall', '--no-deps', archive]]

    def failed(message):
        return {'ok': False, 'state': 'failed', 'version': '', 'message': f'{message} {manual}'}

    try:
        lock = _acquire_lock(Path(data_dir) / LOCK_FILE, now)
    except OSError as error:
        return failed(f'Could not create the update lock in {data_dir}: {error.strerror or error}.')
    if lock is None:
        return {'ok': False, 'state': 'locked', 'version': '',
                'message': 'Another yapp update is already running.'}
    try:
        # A window can wait for idle long after another window installed a release.
        # Recheck under the lock so stale results cannot reinstall or downgrade it.
        incomplete = Path(data_dir) / INCOMPLETE_FILE
        installed = installed_version()
        if compare(installed, release['latest']) == 'current':
            if not incomplete.exists():
                return {'ok': True, 'state': 'current', 'version': installed,
                        'message': f'yapp {installed} is already installed.'}
            if compare(release['latest'], installed) == 'update_available':
                return failed(f'yapp {installed} has an incomplete update; '
                              f'refusing to downgrade to {release["latest"]}.')
            # An equal VERSION can come from the first pip of a failed update.
            # Reinstall that release to repair it instead of claiming success.
        try:
            # Keep this marker on every failure, including process death. VERSION
            # alone cannot tell peer windows that all install steps succeeded.
            incomplete.write_text(release['latest'])
        except OSError as error:
            return failed(f'Could not record update progress: {error}.')
        for command in commands:
            try:
                # A new session keeps terminal Ctrl-C (sent to the TUI's process group)
                # away from pip, so quitting never interrupts an install halfway.
                result = runner(command, capture_output=True, text=True, timeout=APPLY_TIMEOUT,
                                start_new_session=True)
            except (OSError, subprocess.SubprocessError) as error:
                return failed(f'{type(error).__name__} while installing yapp {release["latest"]}.')
            if result.returncode:
                return failed(f'Install failed: {_tail(result.stderr or result.stdout)}.')
        try:
            # -I: an unrelated yapp in the cwd or PYTHONPATH must not answer for this install.
            probe = runner([python, '-I', '-c', _VERSION_PROBE], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT, start_new_session=True)
            version = probe.stdout.strip() if probe.returncode == 0 else ''
        except (OSError, subprocess.SubprocessError):
            version = ''
        if compare(version, release['latest']) != 'current' or compare(release['latest'], version) != 'current':
            return failed(f'Installed version is {version or "unknown"}, expected {release["latest"]}.')
        try:
            incomplete.unlink()
        except OSError as error:
            return failed(f'Could not complete update progress: {error}.')
        return {'ok': True, 'state': 'installed', 'version': version,
                'message': f'Installed yapp {version}.'}
    finally:
        lock.unlink(missing_ok=True)


def relaunch_path(data_dir, pid=None):
    """This process's relaunch file; execv keeps the pid, but the path travels in RELAUNCH_ENV."""
    return Path(data_dir) / RELAUNCH_FILE.format(pid=os.getpid() if pid is None else pid)


def save_relaunch_state(data_dir, *, drafts, session_id, channel, notices, now=time.time, pid=None):
    """Write this process's drafts/selection privately and atomically; return the path."""
    payload = {'saved_at': now(), 'session_id': session_id, 'channel': channel,
               'notices': list(notices),
               'drafts': [{'key': list(key), 'text': text, 'cursor': cursor}
                          for key, text, cursor in drafts]}
    path = relaunch_path(data_dir, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(payload, handle)
    os.replace(temporary, path)
    return path


def _clean_stale_relaunch_files(data_dir):
    """Remove leftovers from relaunches that never happened (crashes, failed execv)."""
    try:
        entries = list(Path(data_dir).glob('relaunch_state.*.json*'))
    except OSError:
        return
    for entry in entries:
        name = entry.name.removesuffix('.tmp')
        if not _RELAUNCH_NAME.match(name):
            continue
        try:
            if time.time() - entry.stat().st_mtime > RELAUNCH_MAX_AGE:
                entry.unlink(missing_ok=True)
        except OSError:
            pass


def load_relaunch_state(data_dir, *, path=None, now=time.time):
    """Return the saved drafts/selection at `path` once, or None; that file is always removed.

    Only a relaunch file directly inside data_dir is read (and deleted); without a
    path (a normal start) nothing is loaded. Stale leftovers are cleaned up either way.
    """
    payload = None
    try:
        if path:
            path = Path(path)
            if path.parent.resolve() != Path(data_dir).resolve() or not _RELAUNCH_NAME.match(path.name):
                path = None
            else:
                try:
                    payload = json.loads(path.read_text())
                except (OSError, ValueError):
                    payload = None
                finally:
                    path.unlink(missing_ok=True)
    finally:
        _clean_stale_relaunch_files(data_dir)
    if not path:
        return None
    try:
        age = now() - float(payload['saved_at'])
        if not 0 <= age <= RELAUNCH_MAX_AGE:
            return None
        drafts = []
        for item in payload['drafts']:
            key, text, cursor = item['key'], item['text'], item['cursor']
            if (not isinstance(key, list) or len(key) != 2 or not all(isinstance(k, str) for k in key)
                    or not isinstance(text, str) or not isinstance(cursor, int)):
                return None
            drafts.append(((key[0], key[1]), text, cursor))
        session_id, channel = payload.get('session_id'), payload.get('channel')
        notices = payload.get('notices', [])
        if (not isinstance(session_id, (str, type(None))) or not isinstance(channel, (str, type(None)))
                or not isinstance(notices, list) or not all(isinstance(n, str) for n in notices)):
            return None
    except (TypeError, KeyError, ValueError):
        return None
    return {'session_id': session_id, 'channel': channel, 'drafts': drafts, 'notices': notices}
