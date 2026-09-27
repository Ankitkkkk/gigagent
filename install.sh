#!/usr/bin/env bash
# yapp installer: sets up .venv and a shell command named "yapp" or "goon".
#
#   ./install.sh                 # asks for the command name (default: yapp)
#   ./install.sh --name goon     # non-interactive
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${YAPP_BIN_DIR:-$HOME/.local/bin}"
MARKER="# yapp launcher"
NAME=""

usage() { echo "usage: ./install.sh [--name yapp|goon]"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --name) NAME="${2:-}"; shift 2 ;;
        --name=*) NAME="${1#--name=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [ -z "$NAME" ]; then
    if [ -t 0 ]; then
        read -r -p "Command name to install [yapp/goon] (default: yapp): " NAME
    fi
    NAME="${NAME:-yapp}"
fi
case "$NAME" in
    yapp|goon) ;;
    *) echo "Command name must be 'yapp' or 'goon', got '$NAME'." >&2; exit 2 ;;
esac

PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "Python 3.11 or newer is required (tried '$PYTHON')." >&2
    exit 1
fi
command -v tmux >/dev/null 2>&1 || echo "Warning: tmux not found; agent spawn/attach will be unavailable." >&2

cd "$ROOT"
if [ ! -x .venv/bin/python ]; then
    echo "Creating virtual environment in $ROOT/.venv"
    "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -q -r requirements-cli.txt

TARGET="$BIN_DIR/$NAME"
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    if ! grep -qF "$MARKER" "$TARGET" 2>/dev/null; then
        echo "$TARGET already exists and is not a yapp launcher; not overwriting it." >&2
        exit 1
    fi
fi
mkdir -p "$BIN_DIR"
q() { printf '%q' "$1"; }
cat > "$TARGET" <<LAUNCHER
#!/bin/sh
$MARKER
cd $(q "$ROOT") || exit 1
YAPP_COMMAND=$NAME exec $(q "$ROOT/.venv/bin/python") $(q "$ROOT/yapp.py") "\$@"
LAUNCHER
chmod 755 "$TARGET"
echo "Installed $TARGET"

case ":$PATH:" in
    *":$BIN_DIR:"*) echo "Run '$NAME' to start." ;;
    *) echo "Add $BIN_DIR to PATH, e.g.: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
