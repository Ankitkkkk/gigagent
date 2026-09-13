# Repository Guide

## Scope and Purpose

This file applies to the entire `agentchattr` repository. Run commands from this
directory, which contains `run.py` and `requirements.txt`; the enclosing
`agent-collab` directory is not the Git root.

Agentchattr is a local chat application for humans and AI coding agents. It
provides channels, mentions, agent presence and roles, jobs, rules, structured
sessions, schedules, image sharing, and history import/export. Agents communicate
through MCP or the HTTP API; wrappers consume queued triggers and deliver prompts
to agent terminals or API-backed agents.

## Setup and Commands

Use Python 3.11 or newer. Runtime dependencies are FastAPI, Uvicorn, and MCP.
Keep the `mcp<2.0` constraint in `requirements.txt`: this code imports
`mcp.server.fastmcp`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.
The default listeners are the web UI at `http://127.0.0.1:8300`, MCP
streamable HTTP at `http://127.0.0.1:8200/mcp`, and MCP SSE at
`http://127.0.0.1:8201/sse`.

Use `run.py` for the complete application: it wires shared stores into MCP,
starts both MCP transports, serves the index with its session token, and mounts
static files. Starting only `uvicorn app:app` skips that initialization.

Platform launchers live in `macos-linux/` and `windows/`. Unix terminal agents
require tmux and the selected provider's CLI. `wrapper_api.py` runs API-backed
agents. Inspect launcher and wrapper behavior before running them: they can
start agent processes and modify provider MCP configuration.

For an isolated manual check, choose three unused ports and separate data and
upload directories, for example:

```sh
.venv/bin/python run.py --port 18300 --mcp-http-port 18200 --mcp-sse-port 18201 --data-dir /tmp/agentchattr-check/data --upload-dir /tmp/agentchattr-check/uploads
```

Use matching overrides for every wrapper connecting to that instance.

## Architecture

- `run.py`: application startup, shared dependency wiring, HTTP/MCP listeners,
  static assets, and browser session token injection.
- `app.py`: FastAPI routes, WebSocket protocol, authentication middleware,
  store callbacks, background checks, and workflow integration.
- `mcp_bridge.py`: agent tools, authenticated identity resolution, read cursors,
  roles, presence, and channel/job context fallback.
- `registry.py`: runtime identities, registration tokens, labels, slots,
  renames, persistence, and recovery after reconnects.
- `router.py`: mention parsing and per-channel agent conversation loop guards.
- `agents.py`: JSONL trigger queues. `wrapper.py`, `wrapper_unix.py`, and
  `wrapper_windows.py` handle CLI integration and terminal delivery;
  `wrapper_api.py` handles API agents; `mcp_proxy.py` forwards MCP requests.
- `store.py`: JSONL message persistence and observers. `jobs.py`, `rules.py`,
  `summaries.py`, and `schedules.py` manage their respective persisted records.
- `session_store.py`, `session_engine.py`, and `session_templates/`: session
  persistence, templates, role casting, phases, and turn sequencing.
- `providers/` — provider adapters (spec §6). Add a vendor by subclassing `providers.base.ProviderAdapter`
  in a module on PYTHONPATH (or in `adapters/`, gitignored) and setting `adapter = "module:Class"` in that
  agent's `[agents.<name>]` table.
- `workspace_store.py`, `workspace_unread.py`, `workspace_launcher.py` — terminal sessions (the user-facing
  word is "session"; code says "workspace"). Design: docs/superpowers/specs/2026-09-12-terminal-sessions-design.md
- `archive.py`: versioned ZIP history export/import and deduplication.
- `static/`: plain HTML, CSS, and JavaScript without a frontend build step.
  `chat.js` contains the main client; feature scripts handle channels, jobs,
  sessions, and rules. `core.js` exposes `Hub` events and `store.js` exposes
  reactive `Store` state.
- `tests/`: Python unittest regressions and a separate browser test script.
- `cli.py`: interactive human terminal client and shell chat/session commands.
  `cli_api.py` owns authenticated localhost HTTP and token handling;
  `cli_workspaces.py` owns API-backed session/agent resolution and shell actions;
  `cli_workspace_chat.py` owns server auto-start, session selection, interactive
  lifecycle commands, checkpoints, and terminal handoff. User-facing "session"
  maps to API "workspace". Optional terminal dependencies live in
  `requirements-cli.txt`. Shell sends use WebSocket request IDs and server
  acknowledgments; preserve those when changing message handling.
- `cli_view_contracts.py`: dependency-free immutable view, action, and submit
  values shared by terminal layers. `cli_tui_state.py` owns bounded drafts,
  notices, and viewport anchors. `cli_tui_view.py` owns sanitized prompt-toolkit
  rendering and responsive controls. `cli_tui_dialogs.py` owns awaitable forms,
  confirmations, navigation, and action workflows. `cli_tui.py` composes the
  full-screen application, transport lifetime, signals, quit, and terminal
  handoff. Keep its import from `cli.py` lazy so either module import order and
  script entry remain safe.

## Configuration and Local State

`config_loader.py` is shared by the server and wrappers. Precedence is:

1. `config.toml` supplies defaults.
2. `config.local.toml` adds new entries under `[agents]` only. It does not
   override existing agent definitions or other configuration sections.
3. `AGENTCHATTR_DATA_DIR`, `AGENTCHATTR_PORT`, `AGENTCHATTR_MCP_HTTP_PORT`,
   `AGENTCHATTR_MCP_SSE_PORT`, and `AGENTCHATTR_UPLOAD_DIR` override the
   corresponding settings.
4. Explicit CLI override flags set those environment variables before loading.
   Arguments after `--` are passed through to the agent CLI.

Relative paths supplied through environment/CLI overrides resolve against the
invocation's working directory. Prefer absolute paths for isolated instances.
Keep private configuration, credentials, runtime data, uploads, virtual
environments, logs, and generated release archives out of commits. Consult
`.gitignore` and `config.local.toml.example` before adding local files.

## Change Guidelines

- Follow the existing Python modules and plain JavaScript feature structure.
  Preserve script ordering in `static/index.html`; feature scripts share globals
  and depend on `Hub`, `Store`, and the main client. Follow existing asset query
  versioning when changing browser assets.
- Keep HTTP, MCP, WebSocket payloads, and browser handlers consistent when
  changing shared behavior. Check live updates as well as history reloads.
- Derive agent identity from registration tokens. Preserve the distinction
  between browser session tokens and agent bearer tokens, and retain origin
  checks and localhost defaults.
- Preserve full instance handles such as `claude-2`, channel-scoped loop guards,
  and explicit channel/job routing. Identity or channel changes must also
  migrate dependent cursors, roles, and fallback state where applicable.
- Preserve persistence compatibility, record IDs, archive deduplication, and
  reference remapping. Imported history must not trigger new agent activity.
- Keep store locking and callback boundaries intact. Store callbacks can run
  from MCP/background threads; use the existing event-loop bridge when
  scheduling WebSocket broadcasts.
- Preserve both Unix and Windows wrapper interfaces. Terminal injection changes
  must retain bounded timeouts, cleanup on failure, and correct paste/Enter
  sequencing.
- Use temporary directories and mocked provider calls for tests. Avoid using a
  person's active chat history or launching paid agent sessions for validation.

## Verification

The test runner is the standard library's `unittest`; pytest is not required.

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s tests -p 'test_router.py' -v
```

Install `requirements-cli.txt` to exercise the interactive terminal tests.
`tests/test_cli_integration.py` starts an isolated server on temporary ports and
uses temporary data, with no provider CLIs. Its prompt test uses simulated
terminal input; real terminal rendering and Windows behavior need manual checks.
`tests/_cli_server.py` is the shared setup-only fixture; do not inherit test
methods into another suite. `tests/test_cli_workspaces_integration.py` covers
real session HTTP/chat, prompt-toolkit pipe input, and tmux lifecycle using only
an inert PATH-shim `kilo`. It also checks auto-start against a pre-existing
isolated tmux server with changed port/data/upload environment overrides.

Keep three temporary ports, temporary data/uploads, and unique `TMUX_TMPDIR`
for integration tests. Remove inherited `TMUX` so it cannot select a developer
socket. Register child cleanup before readiness waits, terminate/kill then wait,
and kill only the isolated tmux server. Redact token query values and startup
token lines from failure log excerpts before temporary-directory cleanup.
Never replace the inert shim with paid Claude/Codex sessions. Foreground attach
and buffered WebSocket output use controlled terminal tests in
`tests/test_cli_workspace_chat.py`; the real tmux test covers lifecycle only.
Full-screen component and application tests use prompt-toolkit pipe input with
captured VT screens from `tests/_tui_harness.py`. For isolated PTY QA, preserve
the real CLI entry, terminal dimensions, input bytes, and terminal restoration;
capture runtime cells instead of treating stripped ANSI output as a screen.
Always remove inherited `TMUX`, use a private `TMUX_TMPDIR`, and register child
and private tmux-server cleanup before launch. Never attach to or kill a
developer tmux server.

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_cli*integration.py' -v
```

CLI usage currently requires a source checkout: `build_release.py` does not
yet include CLI files/dependencies or terminal-session core modules/providers.
That pre-existing packaging gap remains outside this CLI slice.

Run focused regressions for narrow changes and the full suite for shared
routing, identity, persistence, transport, or workflow changes. Tests include
local HTTP servers and Unix tmux transport checks; report platform or dependency
skips explicitly. Tests that patch `app` or `mcp_bridge` globals should restore
them using the existing fixture patterns.

For browser voice-input changes, install Playwright and Chromium in a test
environment, start an isolated server, and run:

```sh
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser/voice_typing.py http://127.0.0.1:18300
```

That script sends a test message and mocks the speech recognition API; it does
not validate microphone hardware or a real speech service. For other UI changes,
check the affected workflow in a browser, including refresh and WebSocket updates.

Report what was actually exercised. Passing unit/integration tests does not
establish that every provider CLI, Windows launcher, browser interaction, or
external API works end to end.
