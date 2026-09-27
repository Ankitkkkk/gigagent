#!/usr/bin/env python3
"""yapp: the terminal UI and shell client for the local yapp server."""

import os

from cli import main


if __name__ == '__main__':
    # install.sh sets YAPP_COMMAND so help text matches the chosen command (yapp or goon).
    main(prog=os.environ.get('YAPP_COMMAND') or 'yapp')
