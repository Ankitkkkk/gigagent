#!/usr/bin/env python3
"""yapp: the terminal UI and shell client for the local yapp server.

Installed as both the `yapp` and `goon` commands.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMMAND_NAMES = ('yapp', 'goon')


def _user_data_dir() -> Path:
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local'
    else:
        base = os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share'
    return Path(base) / 'yapp'


def main():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    # A source checkout keeps ./data and ./uploads next to the code. An installed
    # package must not write into site-packages (reinstalls would wipe it).
    if not (ROOT / 'pyproject.toml').exists():
        data = _user_data_dir()
        os.environ.setdefault('YAPP_DATA_DIR', str(data / 'data'))
        os.environ.setdefault('YAPP_UPLOAD_DIR', str(data / 'uploads'))
    name = Path(sys.argv[0]).name
    from cli import main as cli_main
    cli_main(prog=name if name in COMMAND_NAMES else 'yapp')


if __name__ == '__main__':
    main()
