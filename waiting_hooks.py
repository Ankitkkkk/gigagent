"""Install provider notification hooks or relay one event to its wrapper.

The emit command always exits silently without a decision. It has no network
access and never includes tool inputs in the private per-launch event stream.
"""
import argparse
from dataclasses import asdict
import json
from itertools import islice
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import uuid

from providers import get_adapter

EVENTS_ENV = 'AGENTCHATTR_PROMPT_EVENTS'


def configure_stream(directory: Path, provider: str, agent_cfg: dict):
    """Remember the launch's adapter identity, never its credentials/options."""
    data = {'provider': provider, 'adapter': agent_cfg.get('adapter')}
    path = directory / '.adapter'
    with path.open('x') as stream:
        os.chmod(path, 0o600)
        json.dump(data, stream)


def emit(provider: str, payload: dict, directory: Path, *, now=None) -> bool:
    if not isinstance(payload, dict) or not directory.is_dir():
        return False
    config = {}
    manifest = directory / '.adapter'
    if manifest.exists():
        if manifest.is_symlink() or manifest.stat().st_size > 4096:
            return False
        launch = json.loads(manifest.read_text())
        if not isinstance(launch, dict) or not isinstance(launch.get('provider'), str):
            return False
        provider = launch['provider']
        if launch.get('adapter'):
            if not isinstance(launch['adapter'], str):
                return False
            config['adapter'] = launch['adapter']
    event = get_adapter(provider, config).prompt_event(payload)
    if event is None or not event.valid():
        return False
    # Bounded notification backlog; the terminal detector remains available.
    if sum(1 for _ in islice(directory.glob('*.json'), 256)) >= 256:
        return False
    at = time.time() if now is None else now
    data = json.dumps({'at': at, 'event': asdict(event)}).encode()
    fd, tmp = tempfile.mkstemp(prefix='.event-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(tmp, directory / f'{time.time_ns():020d}-{uuid.uuid4().hex}.json')
    finally:
        Path(tmp).unlink(missing_ok=True)
    return True


def install(provider: str, project: Path) -> Path:
    if provider != 'codex':
        raise ValueError(f'Hook installation is not supported for {provider}')
    project = project.resolve()
    if not project.is_dir():
        raise ValueError('Project directory does not exist')
    folder = project / '.codex'
    path = folder / 'hooks.json'
    if folder.is_symlink() or path.is_symlink():
        raise ValueError('Refusing to replace a symlinked hook configuration')
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        raise ValueError('Expected a hooks JSON object')
    hooks = data.setdefault('hooks', {})
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), 'emit', '--provider', provider])
    definitions = get_adapter(provider).prompt_hook_config(command)
    for event, groups in definitions.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list) or any(not isinstance(group, dict) for group in existing):
            raise ValueError(f'Invalid hook groups for {event}')
        for group in groups:
            if group not in existing:
                existing.append(group)
    serialized = json.dumps(data, indent=2) + '\n'
    if path.exists() and path.read_text() == serialized:
        return path
    folder.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.hooks-', dir=folder)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(serialized)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('emit', 'install'))
    parser.add_argument('--provider', default='codex')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.action == 'emit':
        try:
            directory = os.environ.get(EVENTS_ENV)
            if directory:
                emit(args.provider, json.loads(sys.stdin.read(131072)), Path(directory))
        except Exception:
            pass  # reporting must not change the native permission decision
        return
    try:
        path = install(args.provider, args.project)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Could not install waiting hooks: {error}\n')
    print(f'Installed: {path}\nReview and trust these commands using Codex /hooks.\n'
          'New agentchattr wrappers enable their private event stream. No approvals are automated.')


if __name__ == '__main__':
    main()
