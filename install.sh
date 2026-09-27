#!/bin/sh
# yapp installer: puts yapp in its own virtual environment and adds the
# `yapp` and `goon` commands to ~/.local/bin. Run it again to update.
#
#   curl -fsSL https://yapp.riggedcode.com/install.sh | sh
#
# Environment overrides:
#   YAPP_SOURCE    what to pip-install (default: the main branch archive on GitHub)
#   YAPP_HOME      install location (default: ${XDG_DATA_HOME:-~/.local/share}/yapp)
#   YAPP_BIN_DIR   where the commands go (default: ~/.local/bin)
#   PYTHON         Python 3.11+ interpreter to use (default: python3)
set -eu

SOURCE="${YAPP_SOURCE:-https://github.com/Ankitkkkk/yapp/archive/refs/heads/main.zip}"
YAPP_HOME="${YAPP_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/yapp}"
VENV="$YAPP_HOME/venv"
BIN_DIR="${YAPP_BIN_DIR:-$HOME/.local/bin}"
PYTHON="${PYTHON:-python3}"

say() { printf '%s\n' "$*"; }
fail() { printf 'yapp install: %s\n' "$*" >&2; exit 1; }

command -v "$PYTHON" >/dev/null 2>&1 \
    || fail "$PYTHON not found. Install Python 3.11 or newer (Ubuntu/Debian: sudo apt install python3 python3-venv; macOS: brew install python)."
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || fail "Python 3.11 or newer is required; $PYTHON is $("$PYTHON" -c 'import platform; print(platform.python_version())'). Set PYTHON=/path/to/python3.11 and retry."
command -v tmux >/dev/null 2>&1 \
    || say "Warning: tmux is not installed. yapp needs it to start the server and agents (sudo apt install tmux / brew install tmux)."

if [ ! -x "$VENV/bin/python" ]; then
    say "Creating environment in $VENV"
    mkdir -p "$YAPP_HOME"
    "$PYTHON" -m venv "$VENV" 2>/dev/null || {
        rm -rf "$VENV"
        fail "could not create a virtual environment. On Ubuntu/Debian run: sudo apt install python3-venv (or python3.X-venv matching your Python), then rerun this installer."
    }
fi

say "Installing yapp from $SOURCE"
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q --upgrade "$SOURCE"
# The version number does not change on every commit, so force the app
# itself to reinstall (dependencies are left alone) to pick up updates.
"$VENV/bin/python" -m pip install -q --force-reinstall --no-deps "$SOURCE"

mkdir -p "$BIN_DIR"
linked=""
links=""
for name in yapp goon; do
    target="$BIN_DIR/$name"
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ "$(readlink "$target" 2>/dev/null)" != "$VENV/bin/$name" ]; then
            say "Skipped $target: something else already uses that name."
            continue
        fi
    fi
    ln -sf "$VENV/bin/$name" "$target"
    linked="$linked${linked:+ or }'$name'"
    links="$links \"$target\""
done
[ -n "$linked" ] || fail "both $BIN_DIR/yapp and $BIN_DIR/goon are taken; run $VENV/bin/yapp directly or set YAPP_BIN_DIR."

say "Installed yapp $("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("yapp"))')."
case ":$PATH:" in
    *":$BIN_DIR:"*) say "Run $linked to start." ;;
    *) say "Add $BIN_DIR to your PATH, then open a new terminal and run $linked:"
       say "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.$(basename "${SHELL:-bash}")rc" ;;
esac
say "To update later, run this installer again. To uninstall: rm -rf \"$VENV\"$links"
