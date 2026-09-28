# Self-update: design

Date: 2026-09-27
Status: approved in conversation; spec awaiting review
Revised during implementation: per-process relaunch state (several TUIs no longer share one file)

## Goal

People who installed yapp should get new versions automatically. When a new
release is published, a running TUI installs it, restarts the server, and
reopens itself on the new version, with agents still running and unsent drafts
restored. Success: users stay current without doing anything.

Decisions made with the user:

- A "new version" means a **published GitHub Release** of `Ankitkkkk/yapp`
  (tag `vX.Y.Z`). Commits to `main` alone never prompt.
- Updates are **automatic and immediate**: as soon as a running TUI finds a
  release, it installs it, restarts the server, and relaunches itself. No
  confirmation. Users can turn automatic installs off (then they get a notice
  and the F4 command instead).

## Non-goals

- Updating when no TUI is running (shell commands such as `yapp status` never update).
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
  6 hours; `force=True` bypasses the cache. Network errors, timeouts
  (5 seconds), rate limits, and malformed JSON yield `unknown` and are
  cached for 1 hour so an offline machine does not retry on every launch.
- Opt-outs, in `config.toml` / `config.local.toml` or the environment:
  - `[updates] auto = false` or `YAPP_NO_AUTO_UPDATE=1`: keep checking, but
    never install without being asked (notice + F4 command only).
  - `[updates] check = false` or `YAPP_NO_UPDATE_CHECK=1`: no background
    checks at all; `check()` makes no network request and returns `disabled`.
  `yapp update` and F4 → Update yapp still work when invoked explicitly (an
  explicit command is consent to one request).

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

### Checking

The TUI runs `updates.check()` in a background thread once the UI is up, and
again every 6 hours while it stays open. Checks never block the UI.

### Automatic update (default)

When a check returns `update_available`, the install method is `installer` or
`pipx`, and automatic updates are not turned off:

1. **Wait for a safe moment.** If the user is attached to an agent terminal
   (F6 handoff), has a dialog or menu open, or a mutation/action is in
   flight, wait until all of those have finished and the TUI is back on the
   main screen. Re-check every 2 seconds.
2. **Announce.** Show the notice `Updating yapp to <latest>… (agents keep
   running)` and keep the UI responsive while installing.
3. **Install.** Run `apply()` in a thread. On failure, show
   `Automatic update to <latest> failed: <reason>. F4 → Update yapp to retry.`
   and do not retry automatically for this release during this TUI run (the
   old version keeps running).
4. **Restart the server.** Wait for a safe moment again first (installing can
   take minutes, and the user may have started something new since step 1).
   If the connected server is local and reports `restart_supported`, restart
   it through the existing restart action and wait for readiness. If that
   fails, continue; the relaunched TUI will show `Restart the server to
   finish the update (F4 → Restart server).` Wait for a safe moment once more
   before relaunching, in case a dialog was opened while installing.
5. **Save drafts.** Write every unsent draft (destination, text, cursor) and
   the selected session/channel to this process's own
   `<data_dir>/relaunch_state.<pid>.json` (mode 0600). Each process writes
   its own file, so several TUIs updating around the same time never share
   or clobber one another's state.
6. **Relaunch.** Set the `YAPP_RELAUNCH_STATE` environment variable to this
   process's relaunch file path, exit the TUI cleanly, and `os.execv` the
   same command (`sys.argv`); the relaunched process inherits the
   environment and so finds its own state file regardless of pid reuse. The
   relaunch happens in `cli.main` after the TUI returns a "relaunch" result,
   never from inside the running event loop.
7. **Restore.** On startup, `cli.main` reads and clears `YAPP_RELAUNCH_STATE`
   before anything else runs. If it names a relaunch file inside `data_dir`
   that exists and is less than 10 minutes old, the TUI restores those drafts
   and the selection, shows `Updated to yapp <version>.`, and deletes the
   file. Older or unreadable files are deleted without restoring. Stale
   `relaunch_state.<pid>.json` leftovers from crashed or interrupted
   relaunches (no matching env var) are swept up and deleted the same way.

Before step 3, the TUI imports every module it needs for steps 4 to 6 so that
files replaced by the install cannot change code mid-sequence.

### Manual update: F4 → Update yapp

Always listed in F4 Commands. It runs `check(force=True)`:

- Already current: `yapp <version> is up to date.`
- Unknown: shows the reason.
- `checkout` / `unknown` install: shows the instruction message only.
- Otherwise: a confirmation dialog (current → latest, release notes URL,
  "Installs the update, restarts the server, and reopens yapp. Agents keep
  running and drafts are kept."), default No. On yes it runs steps 2 to 7
  above without waiting.

When automatic updates are off, a found update only shows the notice
`yapp <latest> is available. F4 → Update yapp` once per TUI run.

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
  refuses to run twice. If several TUIs are open, the first one to take the
  lock updates; the others see the lock, wait for it to clear, then each
  relaunches itself (steps 5 to 7) once the installed version has changed,
  saving its own drafts to its own `relaunch_state.<pid>.json` rather than a
  file shared with the other TUIs.

## Testing

- `tests/test_updates.py`: version parsing and comparison; `check()` with a
  fake HTTP opener (update available, current, malformed, network error,
  cache hit, cache expiry, opt-out via env and config); `install_method()`
  for each method using temporary directories; `apply()` command
  construction with a fake runner (no real installs); lock handling.
- CLI tests for `yapp update --check` exit codes, `--json`, and refusal
  without `--yes` when stdin is not a terminal.
- TUI tests (existing harness): automatic update runs apply, restart, draft
  save, and returns the relaunch result; it waits while attached or while a
  dialog is open; a failed install leaves the TUI running with a notice and
  is not retried; `auto = false` only shows the notice; F4 lists Update yapp
  and a declined confirm does nothing; drafts and selection are restored from
  `relaunch_state.<pid>.json` via `YAPP_RELAUNCH_STATE` and stale files are ignored.
- `/api/version_check` returns the new shape.
- Manual end-to-end: install with `install.sh` into a scratch home from a
  local archive, confirm the marker file, then run `yapp update` against a
  fake release pointing at a newer local archive.
