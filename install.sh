#!/bin/sh
# yapp installer: puts yapp in its own virtual environment and adds the
# `yapp` and `goon` commands to ~/.local/bin. Run it again to update.
#
#   curl -fsSL https://yapp.riggedcode.com/install.sh | sh
#
# Environment overrides:
#   YAPP_SOURCE    what to pip-install (default: the latest release archive, or main if there is no release)
#   YAPP_HOME      install location (default: ${XDG_DATA_HOME:-~/.local/share}/yapp)
#   YAPP_BIN_DIR   where the commands go (default: ~/.local/bin)
#   PYTHON         optional Python 3.11+ interpreter for a new environment
# If none is available, install a private Python through uv. System Python and
# shell profiles are left alone; downloaded tools/runtime stay under YAPP_HOME.
set -eu

MAIN_ARCHIVE="https://github.com/Ankitkkkk/yapp/archive/refs/heads/main.zip"
SOURCE="${YAPP_SOURCE:-}"
YAPP_HOME="${YAPP_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/yapp}"
VENV="$YAPP_HOME/venv"
BIN_DIR="${YAPP_BIN_DIR:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
fail() { printf 'yapp install: %s\n' "$*" >&2; exit 1; }

supported_python() {
    command -v "$1" >/dev/null 2>&1 &&
        "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1
}

supported_uv() {
    command -v "$1" >/dev/null 2>&1 &&
        "$1" --no-config python install --no-bin --help >/dev/null 2>&1 &&
        "$1" --no-config python find --no-project --system --managed-python \
            --no-python-downloads --help >/dev/null 2>&1
}

managed_python() {
    say "No Python 3.11+ found. Installing a private Python 3.13 for yapp."
    INSTALL_UV="$(command -v uv 2>/dev/null || true)"
    if ! supported_uv "$INSTALL_UV"; then
        INSTALL_UV="$YAPP_HOME/tools/uv"
        if ! supported_uv "$INSTALL_UV"; then
            command -v curl >/dev/null 2>&1 \
                || fail "curl is required to download a compatible Python. Install curl or set PYTHON to a Python 3.11+ executable."
            UV_SCRIPT="$(mktemp)" || fail "could not create a temporary download file."
            trap 'rm -f "$UV_SCRIPT"' 0
            trap 'exit 1' HUP INT TERM
            curl -fsSL --connect-timeout 15 --max-time 120 \
                https://astral.sh/uv/install.sh -o "$UV_SCRIPT" \
                || fail "could not download uv. Check your connection and retry, or set PYTHON to a Python 3.11+ executable."
            # Unmanaged installation disables PATH/profile changes and self-updates.
            UV_UNMANAGED_INSTALL="$YAPP_HOME/tools" UV_NO_MODIFY_PATH=1 \
                sh "$UV_SCRIPT" || fail "could not install the private Python downloader."
            rm -f "$UV_SCRIPT"
            trap - 0 HUP INT TERM
            supported_uv "$INSTALL_UV" || fail "the private Python downloader cannot run on this system."
        fi
    fi
    # Keep both the runtime and cache out of global Python/uv locations.
    UV_PYTHON_INSTALL_DIR="$YAPP_HOME/python" UV_CACHE_DIR="$YAPP_HOME/cache/uv" \
        "$INSTALL_UV" --no-config python install 3.13 --no-bin \
        || fail "could not install private Python. Check your connection and retry, or set PYTHON to a Python 3.11+ executable."
    PYTHON="$(UV_PYTHON_INSTALL_DIR="$YAPP_HOME/python" UV_CACHE_DIR="$YAPP_HOME/cache/uv" \
        "$INSTALL_UV" --no-config python find --no-project --system --managed-python --no-python-downloads 3.13)" \
        || fail "could not locate the private Python installation."
    supported_python "$PYTHON" || fail "downloaded Python could not run on this system."
}

if [ -x "$VENV/bin/python" ]; then
    # Updates use the environment's interpreter, regardless of system Python.
    supported_python "$VENV/bin/python" \
        || fail "existing environment at $VENV needs Python 3.11+. It was left unchanged. Use a different YAPP_HOME for a fresh installation."
    PYTHON="$VENV/bin/python"
elif [ -e "$VENV" ] || [ -L "$VENV" ]; then
    fail "existing environment at $VENV is incomplete. It was left unchanged. Move it aside or use a different YAPP_HOME, then retry."
elif [ -n "${PYTHON:-}" ]; then
    supported_python "$PYTHON" \
        || fail "PYTHON=$PYTHON must name a working Python 3.11+ executable. Unset PYTHON to select or download one automatically."
else
    PYTHON=""
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python; do
        if supported_python "$candidate"; then
            PYTHON="$candidate"
            break
        fi
    done
    [ -n "$PYTHON" ] || managed_python
fi
say "Using Python $("$PYTHON" -c 'import platform; print(platform.python_version())') ($PYTHON)."
command -v tmux >/dev/null 2>&1 \
    || say "Warning: tmux is not installed. yapp needs it to start the server and agents (sudo apt install tmux / brew install tmux)."

if [ ! -x "$VENV/bin/python" ]; then
    say "Creating environment in $VENV"
    mkdir -p "$YAPP_HOME"
    "$PYTHON" -m venv "$VENV" || {
        rm -rf "$VENV"
        fail "could not create a virtual environment. On Ubuntu/Debian run: sudo apt install python3-venv (or python3.X-venv matching your Python), then rerun this installer."
    }
fi

if [ -z "$SOURCE" ]; then
    # Latest published release; fall back to main when there is none yet.
    TAG="$("$VENV/bin/python" - <<'PY' 2>/dev/null || true
import json, urllib.request
request = urllib.request.Request(
    'https://api.github.com/repos/Ankitkkkk/yapp/releases/latest',
    headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'yapp-installer'})
with urllib.request.urlopen(request, timeout=10) as response:
    tag = json.load(response).get('tag_name', '')
print(tag if isinstance(tag, str) and tag.startswith('v') else '')
PY
)"
    if [ -n "$TAG" ]; then
        SOURCE="https://github.com/Ankitkkkk/yapp/archive/refs/tags/$TAG.zip"
    else
        say "No published release found; installing the latest main branch."
        SOURCE="$MAIN_ARCHIVE"
    fi
fi

say "Installing yapp from $SOURCE"
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q --upgrade "$SOURCE"
# The version number does not change on every commit, so force the app
# itself to reinstall (dependencies are left alone) to pick up updates.
"$VENV/bin/python" -m pip install -q --force-reinstall --no-deps "$SOURCE"

"$VENV/bin/python" - "$VENV/yapp-install.json" "$SOURCE" <<'PY'
import json, sys
from datetime import datetime, timezone
path, source = sys.argv[1], sys.argv[2]
with open(path, 'w') as handle:
    json.dump({'method': 'installer', 'source': source,
               'installed_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}, handle)
PY

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
