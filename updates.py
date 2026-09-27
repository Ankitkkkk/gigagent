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
RELAUNCH_MAX_AGE = 600
CACHE_FILE = 'update_check.json'
LOCK_FILE = 'update.lock'
RELAUNCH_FILE = 'relaunch_state.json'
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
