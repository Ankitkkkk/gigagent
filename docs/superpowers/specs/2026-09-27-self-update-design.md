# Self-update: design

Date: 2026-09-27
Status: approved in conversation; spec awaiting review

## Goal

People who installed yapp should learn when a new version is published and be
able to update in one step without leaving the TUI. Success: one confirmation
takes a user from the old version to the new one running, with their agents
still alive.

Decisions made with the user:

- A "new version" means a **published GitHub Release** of `Ankitkkkk/yapp`
  (tag `vX.Y.Z`). Commits to `main` alone never prompt.
- Accepting an update **installs it, restarts the server, and relaunches the
  TUI automatically**.

## Non-goals

- Automatic updates without confirmation.
- Downgrades, pre-release channels, or picking an arbitrary version.
- Self-updating source checkouts (they get instructions only).
- Publishing to PyPI.

## 1. Update check: `updates.py`

A new top-level module, used by the TUI, the CLI, and the server. It has no
third-party dependencies (uses `urllib`), so it works in every install type.

- `latest_release()` calls
  `https://api.github.com/repos/Ankitkkkk/yapp/releases/latest` and returns the
  tag, version (tag without the leading `v`), release page URL, and the source
  archive URL `https://github.com/Ankitkkkk/yapp/archive/refs/tags/<tag>.zip`.
  Drafts and pre-releases are excluded by that endpoint.
  The API URL can be overridden with `YAPP_UPDATE_URL` (used by tests and the
  manual end-to-end check).
- `current_version()` reads the packaged `VERSION` file.
- `compare(current, latest)` parses dotted integer versions (`0.5.0`,
  `v0.6.1`) into tuples. Anything unparsable yields "unknown", never
  "update available". No `packaging` dependency (it is not a declared
  dependency today).
- `check(force=False)` returns a result with `state` in `update_available`,
  `current`, `unknown`, or `disabled`, plus `current`, `latest`, `url`,
  `archive_url`. Results are cached in `<data_dir>/update_check.json` for
  24 hours; `force=True` bypasses the cache. Network errors, timeouts
  (5 seconds), rate limits, and malformed JSON yield `unknown` and are
  cached for 1 hour so an offline machine does not retry on every launch.
- Opt-out: environment variable `YAPP_NO_UPDATE_CHECK=1`, or
  `[updates] check = false` in `config.toml` / `config.local.toml`. When
  disabled, `check()` makes no network request and returns `disabled`.
  `yapp update` still works when invoked explicitly (an explicit command is
  consent to one request).

The existing `/api/version_check` endpoint in `app.py` (which still points at
the upstream `bcurts/agentchattr` releases and uses fork detection) is
rewritten to return `updates.check()`'s result. The browser "Update available"
pill in `static/chat.js` keeps working, now for yapp releases; its
upstream/fork label branch is removed. `_detect_install_kind`,
`_fetch_latest_release`, and `_compare_versions` in `app.py` are deleted.

## 2. Applying an update: `updates.install_method()` and `apply()`

`install_method()` inspects the running interpreter and package location and
returns one of:

| Method | How it is detected | How `apply(release)` updates |
| --- | --- | --- |
| `installer` | `sys.prefix/yapp-install.json` exists (written by `install.sh`) | `sys.executable -m pip install --upgrade <archive_url>`, then `--force-reinstall --no-deps <archive_url>` |
| `pipx` | `sys.prefix` is inside a `pipx/venvs/yapp` directory and `pipx` is on `PATH` | `pipx install --force <archive_url>` (a zip URL, so `git` is not required) |
| `checkout` | `pyproject.toml` sits next to `yapp.py` | none; message: `git pull`, then restart yapp |
| `unknown` | anything else | none; message shows the installer command |

`apply()` runs the command with a timeout (10 minutes) and captured output,
and returns success or a failure message that includes the last lines of
output and the manual command. It never uses a shell. It re-reads the
installed version afterwards via a fresh `python -c` subprocess and reports
failure if it did not change to the release version.

`install.sh` changes:

- Resolve the latest release via the GitHub API (parsed with the venv's
  Python) and install its tag archive. If no release exists or the API is
  unreachable, fall back to the `main` archive and say so. `YAPP_SOURCE` still
  overrides.
- After a successful install, write `$VENV/yapp-install.json`:
  `{"method": "installer", "source": "<what was installed>", "installed_at": "<UTC ISO time>"}`.

## 3. TUI

- On startup, the TUI runs `updates.check()` in a background thread after the
  UI is up. When `state == update_available`, it shows one notice:
  `yapp <latest> is available. F4 → Update yapp`. It does not repeat the
  notice during that TUI run.
- F4 Commands gains **Update yapp**, always listed. Selecting it:
  1. Runs `check(force=True)`. If already current, shows
     `yapp <version> is up to date.` If unknown, shows the error.
  2. For `checkout` / `unknown` installs, shows the instruction message only.
  3. Otherwise shows a confirmation dialog: current → latest version, the
     release notes URL, and "Installs the update, restarts the server, and
     reopens yapp. Agents keep running. Unsent drafts are lost." Default: No.
  4. On yes: notice `Installing yapp <latest>…`, runs `apply()` in a thread.
     On failure: notice with the message; the old version keeps running.
  5. On success: if the connected server is local and reports
     `restart_supported`, restart it through the existing restart action and
     wait for it to become ready. If restart is unsupported or fails, show a
     notice telling the user to restart the server, and continue.
  6. Exit the TUI cleanly, then `os.execv` the same command (`sys.argv`) so
     the new code loads. The relaunch happens in `cli.main` after the TUI
     returns a "relaunch" result, never from inside the running event loop.

## 4. CLI: `yapp update`

A new subcommand (so `goon update` works too):

- `yapp update` checks (forced), prints current and latest version and the
  release URL, asks `Update now? [y/N]`, applies, then restarts the local
  server if one is running and supports restart. Without a terminal it
  requires `--yes`.
- `yapp update --check` only prints the check result; exit code 0 when
  current, 10 when an update is available, 1 on error.
- `yapp update --yes` skips the question.
- `--json` prints the check/apply result as JSON.

## 5. Release routine (documented in README)

1. Bump `VERSION` (for example to `0.6.0`) and merge to `main`.
2. Create a GitHub Release with tag `v0.6.0` and release notes.

A first release `v0.5.0` must be published so the check has a baseline.
If a release tag does not match `VERSION` on that tag, users see the
tag's version; keeping them in sync is the maintainer's job, and the README
says so.

## Error handling summary

- No network / GitHub down / rate limited: check returns `unknown`; TUI shows
  nothing on startup; explicit update commands show the reason.
- Update install fails: old version keeps running; message includes the
  manual command.
- Server restart fails after a successful install: the new code is installed;
  notice tells the user to restart the server (F4 → Restart server).
- Concurrent updates: `apply()` holds a lock file in the data directory and
  refuses to run twice.

## Testing

- `tests/test_updates.py`: version parsing and comparison; `check()` with a
  fake HTTP opener (update available, current, malformed, network error,
  cache hit, cache expiry, opt-out via env and config); `install_method()`
  for each method using temporary directories; `apply()` command
  construction with a fake runner (no real installs); lock handling.
- CLI tests for `yapp update --check` exit codes, `--json`, and refusal
  without `--yes` when stdin is not a terminal.
- TUI tests (existing harness): F4 lists Update yapp; update-available notice
  on startup; confirm declined does nothing; success path calls apply,
  restart, and returns the relaunch result; failure path leaves the TUI
  running with a notice.
- `/api/version_check` returns the new shape.
- Manual end-to-end: install with `install.sh` into a scratch home from a
  local archive, confirm the marker file, then run `yapp update` against a
  fake release pointing at a newer local archive.
