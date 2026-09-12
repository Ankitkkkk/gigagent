# Terminal Sessions CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete slice 2: a human terminal client for selecting, creating, operating, attaching to, and leaving durable agent sessions through the implemented server APIs.

**Architecture:** Keep `cli.py` as the entry point and `ChatClient` as the existing WebSocket transport/cache. Extract reusable local HTTP/bootstrap primitives to `cli_api.py`; put workspace resolution/API and shell operations in `cli_workspaces.py`; put picker, prompts, session command handling, and attach orchestration in `cli_workspace_chat.py`. The server remains the only writer of records and owner of agent processes.

**Tech Stack:** Python 3.11+, standard-library argparse/urllib/asyncio/subprocess, existing prompt-toolkit and websockets dependencies, unittest, tmux on Unix. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-12-terminal-sessions-design.md` §5, with actual §2/§4 APIs in `app.py` and Claude's slice-2 rulings in `AGENT_MESSAGES.md`.

## Global Constraints

- Work in place on `feature/terminal-sessions`; server-core reviewed baseline `eaa7a2b`. No merge, push, or real provider launch in tests. Preserve other workers' documentation edits and untracked coordination files.
- User-facing word is "session"; persistent/API code says "workspace". Session identifiers are `ws_*`, stable agent identifiers are `ag_*`; tmux target uses server `tmux_session`, falling back to `agentchattr-<agent_id>` only when absent.
- All mutations use authenticated HTTP APIs. Never read/write `workspaces.json`, identity shadows, provider conversations, or registry files in the CLI.
- Keep local-only URL validation, token secrecy, proxy bypass, terminal-control sanitization, existing WebSocket request IDs/acknowledgments, and the existing shell failure conventions.
- Existing `--history N` / `--limit N` are numeric message limits; `--name` remains human sender name. Agent operations use `--agent-name`; history policy uses `--history-mode`. Common flags work before or after subcommands.
- History prompts offer `none` and `literal`, default `literal` at every channel size. Explicit `summary` fails with `summary history mode is not available in this version; use literal or none`. Summary generation and failed-summary prompts remain slice 3.
- Interactive `chat` without `--url` may auto-start a server; shell commands never auto-start. Explicit `--url` never auto-starts. Explicit `--channel` means plain channel chat; `--session` means chosen session; both together are an error; neither in interactive chat means picker. Shell send/read without either retain #general.
- Windows rejects spawn/resume/attach/auto-start with `Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch.` Other HTTP/chat commands continue working against an existing server.
- Full suite interpreter is `/tmp/agentchattr-cli-venv/bin/python`; run from repository root. Baseline 343 tests, 2 expected skips. Focused tests per task, full suite for integration changes; isolated temporary ports/data/TMUX_TMPDIR only.

## Settled behavior and actual API contracts

`GET /api/workspaces?include_archived=0|1` returns `{workspaces: [...], warning?: str}`. Workspace entries expose `last_state` on agents, not a separate live `state`. `GET /api/workspaces/{id}/unread` returns `{agents: [{agent_id, registry_name, count, messages: [{id,sender,text,routed_to:[agent_id]}]}]}`. Spawn/resume/stop/history return agent records; retry returns `{ok:true}`; checkpoint returns `{checked: N}`; archive/unarchive/rename return workspace views. Display `last_state`, `unread_count`, and presence/absence of `native_session_id`.

- Session resolution: exact id, then exact name, then unique name prefix. Duplicate exact names are ambiguous. Errors list sanitized names and ids. Include archived records when explicitly resolving a session so backend refusal/restore can be explained.
- Agent resolution is scoped to one workspace: exact registry_name or agent_id; otherwise unique provider. Ambiguity lists registry names and ids. No global provider-name fallback.
- All values inserted into URL paths are percent-quoted; query strings use urlencode. JSON output is original API data, not sanitized/mutated human output. Diagnostics go to stderr, not JSON stdout.
- Archived picker selection prints `archived` and asks `Unarchive it? [y/N]`; yes calls unarchive then selects; no returns to picker. Explicit `--session` selecting archived asks the same question; no raises CLIError and exits 1. `sessions --archived` includes active and archived records. Merely showing archived never mutates anything.
- Missing cwd gets warning `⚠ cwd missing — /resume <agent> --cwd PATH`; select-time batch resume skips it. Missing native id shows `id unknown`; attempt resume for eligible exited agents, print backend refusal and an exact fresh command, continue other agents. `--no-resume` suppresses the batch question entirely.
- In session chat `/history` without arguments retains recent-message display; `/history AGENT MODE` changes policy. `/join` and `/create` are refused while a session is selected, with a hint to use `/sessions` or plain `--channel` mode; this prevents commands operating one workspace while chat displays another channel.
- Archive API already checkpoints/stops agents; do not issue redundant client checkpoint before archive. `/sessions` checkpoints before picker; `/quit` and EOF checkpoint current session before receiver shutdown. Checkpoint failure prints a warning but cannot trap the user in the CLI or stop agents.
- Readiness output uses `starting`, `running`, `exited` and `catching up…` for literal pending state, never `summarizing` in this slice. New launch exit with last_error prints `failed to start; see <status.data_dir>/logs/wrapper-<agent_id>.log` when data_dir is known. A new Claude spawn with no cwd/.claude state prints a trust-prompt/attach caveat.

## Literal output contract (Tasks 2–5)

Use the exact strings below, substituting angle-bracket placeholders. Whitespace aligning dynamic picker columns may vary; headings, menu labels, prompts, warnings and state labels do not.

```text
Sessions
  1. billing refactor     claude-2 running · codex-1 exited (unread 3)     2h ago
  2. ws_9a1b2c            no agents                                        3d ago
  n. New session
  a. Show archived
Choose:
```

| Situation | Literal text |
|---|---|
| Missing cwd | `⚠ cwd missing — /resume <agent> --cwd PATH` |
| Eligible stopped agents | `Resume N stopped agents? [Y/n]` |
| Explicit archived selection | `archived` followed by `Unarchive it? [y/N]` |
| Successful auto-start | `Started server in tmux session agentchattr-server.` |
| Server unavailable | `Start it manually: python run.py` plus resolved `<data_dir>/logs/server.log` and existing `agentchattr-server` name if present |
| Unsupported Windows operation | `Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch.` |
| Missing tmux agent | `not running; resume with /resume <agent>` (shell hint: `python cli.py resume <agent> --session <session>`) |
| Failed launch | `failed to start; see <data_dir>/logs/wrapper-<agent_id>.log` |
| Unavailable summary | `summary history mode is not available in this version; use literal or none` |
| Literal catch-up pending | `catching up…` |
| Fresh launch still starting | `fresh` |
| Unknown native id | `id unknown` |
| Return from inside-tmux attach | `Switch back: tmux switch-client -l` |

Do not emit a public `checkpoint` shell command. Checkpoint remains an internal API operation for leaving/switching; archive API performs checkpoint itself. `--agent-name` always maps to JSON body `name` on spawn/resume.

## File map and task boundaries

| File | Responsibility | Tasks |
|---|---|---|
| cli_api.py | local URL/bootstrap + synchronous authenticated JSON request | 1 |
| cli_workspaces.py | typed CLI errors, WorkspaceAPI, name resolution, platform guard, shell operation formatting/dispatch | 1,2 |
| cli_workspace_chat.py | server startup, picker, prompt/controller state, session commands and attach | 3,4,5 |
| cli.py | existing transport, parser/main integration, output buffering hooks | 1–5 |
| tests/test_cli_api.py | HTTP transport and resolver regressions | 1 |
| tests/test_cli_workspace_commands.py | parser/shell operations and failures | 2 |
| tests/test_cli_workspace_chat.py | startup/picker/controller/attach unit and async tests | 3–5 |
| tests/_cli_server.py | reusable isolated real-server fixture | 6 |
| tests/test_cli_workspaces_integration.py | CLI through real API/WebSocket/tmux stub | 6 |
| tests/test_cli.py, tests/test_cli_integration.py | preserve/extend original compatibility tests | as needed,6 |
| README.md, AGENTS.md | usage and module documentation | 6 |

### Task 1: Authenticated HTTP and workspace resolution

**Files:** Create `cli_api.py`, `cli_workspaces.py`, `tests/test_cli_api.py`; modify `cli.py` imports/re-exports only.

**Interfaces:** Preserve `cli.local_url`, `cli.SessionTokenParser`, `cli.fetch_session_token`, `cli.get_api`; fetch_session_token gains an optional timeout=5 keyword for bounded bootstrap while old calls remain valid. Define `CLIError(ValueError)` in cli_api.py and re-export it from cli_workspaces.py; it has `message` and optional `status`. `request_json(url, token, method, path, body=None, timeout=5) -> object`. `WorkspaceAPI(url, timeout=15)` exposes `request(method,path,body=None)`, `list(include_archived=False) -> dict`, `get(ws_id) -> dict`, `resolve(selector, include_archived=True) -> dict`, `create(name='') -> dict`, `rename(ws_id,name)`, `action(ws_id, action, agent_id=None, body=None)`, `unread(ws_id,agent_id=None)`, `status()`. `resolve_session(items, selector) -> dict`; `resolve_agent(workspace, selector) -> dict`. Inject/patch opener at transport boundary in tests; no test-only production API.

- [ ] **Write failing resolver and HTTP tests.** Exact resolver example:

```python
from cli_workspaces import CLIError, resolve_session, resolve_agent
rows = [dict(id='ws_a', name='billing', agents=[]),
        dict(id='ws_b', name='billing tools', agents=[])]
self.assertEqual(resolve_session(rows, 'ws_b')['id'], 'ws_b')
self.assertEqual(resolve_session(rows, 'billing')['id'], 'ws_a')
with self.assertRaisesRegex(CLIError, 'ws_a.*ws_b'):
    resolve_session(rows, 'bill')
workspace = {'agents': [dict(agent_id='ag_a', registry_name='claude-1', provider='claude')]}
self.assertEqual(resolve_agent(workspace, 'claude')['agent_id'], 'ag_a')
```

Test duplicate exact names, unknown selectors, multiple provider agents, path quoting, JSON POST/PATCH headers/body, empty successful JSON response, HTTP400 `{error:...}` and HTTP422 `{detail:...}` preserved, 404 `session not found` / `agent not found`, 503 `agent launching is not available on this server`, non-JSON HTTP errors sanitized, timeout/no token leakage, and redirect rejection. Use an isolated stdlib HTTP server for redirect/header/error boundary tests; disable proxies and reject redirects before forwarding credentials, not only after completion. Bootstrap uses local root page and existing parser.

- [ ] **Run red:** `python -m unittest tests.test_cli_api -v`; missing new imports/functions are expected.
- [ ] **Implement transport/extraction and API.** Move existing bootstrap implementations unchanged where possible; `cli.py` re-exports them so existing tests/imports work. `get_api` remains compatible and delegates to JSON GET. Do not retry mutating requests automatically. Reject a changed origin/path in redirects through a no-redirect handler. HTTP errors become `CLIError` carrying server error text/status from `error` or FastAPI `detail` (serialize structured detail readably). Bodiless POSTs use `data=b""` and an explicit method so urllib never changes them to GET. `include_archived` query is the literal `0` or `1`, never Python booleans. General network errors use a stable message without exception URLs. WorkspaceAPI obtains/caches session token via existing browser bootstrap; `request` uses one monotonic deadline across bootstrap and JSON request, passing remaining time to each and refusing to start a request after expiry. On HTTP401/403 invalidate the cached token and propagate the error; the next request bootstraps afresh (no automatic mutation retry). Test token refresh on a subsequent request after server restart/auth failure. Use these path forms:

```python
root = '/api/workspaces/' + quote(ws_id, safe='')
path = root + '/agents/' + quote(agent_id, safe='') + '/' + action
# No agent id: archive/unarchive/checkpoint use root + /action.
# action == spawn uses POST root + /agents (no agent id).
# history/resume bodies are JSON objects; stop/retry have no body.
```

- [ ] **Run green and compatibility:** `python -m unittest tests.test_cli_api tests.test_cli tests.test_cli_integration -v`.
- [ ] **Commit explicit files:** `cli: add shared HTTP API and session resolution`.

### Task 2: Shell session commands and compatible parser

**Files:** Modify `cli.py`, `cli_workspaces.py`; create `tests/test_cli_workspace_commands.py`.

**Interfaces:** `WorkspaceCommandResult(data, workspace=None)` carries original API data and optional already-resolved workspace snapshot. `run_workspace_command(api,args) -> WorkspaceCommandResult` is synchronous and owns HTTP dispatch only. `format_workspace_result(command,result) -> str` returns human-readable text; cli.py applies terminal_text at the output boundary. Do not import cli from helper modules (avoids cycles). `require_tmux_platform()` raises CLIError with exact Windows text. `args.session` and `args.channel` default None; normalized legacy channel is `args.channel or 'general'`. Keep `args.history=30`, name=None, timeout=15,json=False. Add `args.agent_name`, `args.history_mode`, `args.cwd`, `args.fresh`, `args.archived`, `args.yes`, `args.agent`, `args.no_resume`, `args.provider`, `args.session_name` (new NAME), `args.target_session` (archive SESSION) where applicable; avoid collision with existing attributes.

- [ ] **Write parser and dispatch tests.** Existing common flags work both placements:

```python
args = build_parser().parse_args(['--session', 'billing', 'spawn', 'claude',
    '--cwd', '/tmp/project', '--agent-name', 'reviewer', '--history-mode', 'none'])
self.assertEqual(args.history, 30)
self.assertEqual(args.history_mode, 'none')
self.assertEqual(args.agent_name, 'reviewer')
```

Test common url/session/name/history/timeout/json before/after command; `--channel` plus `--session` across parser levels fails before network; history range and timeout validation remain; all nine non-attach new commands dispatch exact method/path/body; send/read session channel resolution preserves send acknowledgment; server400/409 text stderr/exit1; unknown and ambiguous selectors; summary rejected before API; Windows failures before any launch API. `archive` requires `--yes` without a tty, and otherwise confirms y/N. Task5 adds attach syntax and rejects `attach --json` (terminal operation).

- [ ] **Run red:** `python -m unittest tests.test_cli_workspace_commands -v`.
- [ ] **Implement commands and main integration using the literal output contract.** Common parser options are registered at root and command parsers with SUPPRESS child defaults so prior values survive. New shell forms:

```text
sessions [--archived] [--json]
new NAME [--json]
spawn PROVIDER --session S --cwd PATH [--agent-name N] [--history-mode M]
resume AGENT --session S [--fresh] [--agent-name N] [--cwd PATH]
stop AGENT --session S
unread --session S [--agent AGENT]
retry AGENT --session S
history AGENT --session S --mode MODE
archive SESSION [--yes]
```

Add optional `--session` to chat/send/read and `--no-resume` to chat. Every required session selector is validated before network. New NAME uses session_name, never common human name; archive SESSION uses target_session and rejects redundant --session to prevent target ambiguity. Shell `sessions` prints name/id, agent names/states/unread; `new` prints name/id/channel; lifecycle commands print registry_name and last_state; unread prints name/state/count and `#id sender text`; retry success is explicit; checkpoint is internal only. Human unread formatting uses result.workspace for last_state without another fetch. JSON serializes result.data exactly. `send`/`read` reuse existing `shell_command` after resolving channel; status/channels preserve behavior. Use `asyncio.to_thread` for WorkspaceAPI within async command timeout. Shell network failure includes `Start it manually: python run.py`; never starts tmux server. Attach parser/dispatch is added together with its working implementation in Task5, so this task introduces no nonfunctional command.

- [ ] **Run green:** new tests plus existing cli unit/integration; full suite because main/parser changed.
- [ ] **Commit:** `cli: add session shell commands and explicit agent options`.

### Task 3: Interactive server startup and session picker

**Files:** Create `cli_workspace_chat.py`, `tests/test_cli_workspace_chat.py`; modify `cli.py` interactive/main integration.

**Interfaces:** `ensure_server(url, *, explicit_url, config, output=print) -> dict` synchronously returns `/api/status`; only called on interactive path. `async choose_workspace(api,prompt,output,*,selector=None,no_resume=False) -> dict|None`; prompt is `async prompt(text, default='') -> str`; None means EOF/cancelled picker. `WorkspaceChatController(client,api,*,selector=None,no_resume=False,plain_channel=False)` stores `workspace`, with `async initialize(prompt)->bool`. `interactive(client,controller=None)` remains callable as today for plain tests.

- [ ] **Write startup/picker tests.** Stub only HTTP/process/time boundaries, use real resolution/API functions. Concrete picker behavior:

```python
answers = iter(['n', 'billing'])
async def prompt(text, default=''):
    return next(answers)
api.list.return_value = {'workspaces': [{'id':'ws_old','name':'old','agents':[]}]}
api.create.return_value = {'id':'ws_new','name':'billing','channel':'ws-new','agents':[]}
selected = await choose_workspace(api, prompt, output.append, no_resume=True)
self.assertEqual(selected['id'], 'ws_new')
api.create.assert_called_once_with('billing')
```

Test empty list immediately asks name; default blank name; numeric selection; a toggles archived; archived selection unarchive y/N; warning printed; relative age text; missing cwd warning and skipped batch resume; one Y/n batch prompt for eligible exited agents; one refusal does not prevent next resume; exact fresh hint; --session skip picker but retains resume prompt; --no-resume no prompt; EOF exits without create/resume. Startup tests cover already-running, explicit URL never launches, shell path never calls ensure_server, Windows, missing tmux, exact tmux name, launch failure, existing tmux session startup race, 15-second deadline, foreign data_dir warning, explicit resolved-config launch flags, and log hint.

Concrete contract tests (use unittest fixtures for temporary cwd/config and mocked transport/process boundaries):

```python
async def test_picker_exact_menu_and_resume_prompt(self):
    ws = {'id': 'ws_abcd00', 'name': 'billing', 'channel': 'ws-billing',
          'agents': [{'agent_id': 'ag_a', 'registry_name': 'claude-1',
                      'provider': 'claude', 'last_state': 'exited',
                      'cwd': str(self.tmp_path), 'native_session_id': None}]}
    api = Mock()
    api.list.return_value = {'workspaces': [ws]}
    api.get.return_value = ws
    prompts, output = [], []
    async def prompt(text, default=''):
        prompts.append(text)
        return '1' if text == 'Choose:' else 'n'
    selected = await choose_workspace(api, prompt, output.append)
    self.assertEqual(selected['id'], ws['id'])
    rendered = '\n'.join(output)
    self.assertIn('Sessions', rendered)
    self.assertIn('n. New session', rendered)
    self.assertIn('a. Show archived', rendered)
    self.assertIn('Resume 1 stopped agents? [Y/n]', prompts)
    api.action.assert_not_called()

async def test_missing_cwd_warning_skips_resume(self):
    ws = self.workspace_with_agent(cwd='/definitely/missing/cli-cwd', last_state='exited')
    api = self.api_for(ws)
    output = []
    await choose_workspace(api, AsyncMock(return_value='1'), output.append)
    self.assertIn('⚠ cwd missing — /resume claude-1 --cwd PATH', '\n'.join(output))
    api.action.assert_not_called()

async def test_explicit_archived_decline_fails(self):
    api = self.api_for(self.workspace(archived=True))
    prompt = AsyncMock(return_value='n')
    with self.assertRaises(CLIError):
        await choose_workspace(api, prompt, self.output.append, selector='billing')
    prompt.assert_awaited_once_with('Unarchive it? [y/N]', default='n')
    api.action.assert_not_called()
```

For startup tests, after arranging status-down then ready and a successful tmux runner, assert `self.assertIn('Started server in tmux session agentchattr-server.', output)`. For unreachable/timeout cases assert both `Start it manually: python run.py` and `str(data_dir / 'logs/server.log')` in the raised CLIError. Windows assertion is exactly `Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch.`. Parse the actual spawned command with shlex to verify all five resolved flags; do not only assert a mock was called.

- [ ] **Run red:** `python -m unittest tests.test_cli_workspace_chat -v`.
- [ ] **Startup red/green step.** Add failing startup tests, implement, then run focused startup tests. Use the literal output contract. Resolve repo root from module path. Probe local `/api/status` using a fresh browser bootstrap token and bounded HTTP timeout; validate status shape identifies an agentchattr server. Only a transport-unreachable endpoint counts as down; a responding non-agentchattr endpoint fails clearly without auto-start. If down and explicit_url: fail with manual hint. Otherwise Windows/tmux guard, create data_dir/logs and append `server.log`, start `tmux new-session -d -s agentchattr-server -c <ROOT> <safely quoted Python/run.py command with log redirection>`. Use shell quoting for every path. Pass explicit `--port`, `--data-dir`, `--upload-dir`, `--mcp-http-port`, `--mcp-sse-port` values resolved from CLI config (including AGENTCHATTR overrides); resolve relative paths against ROOT. Never rely on the existing tmux server inheriting the caller environment. If `agentchattr-server` exists while the port is down, exit 1 naming it and printing the manual command/log path; never create or kill it. Poll status until monotonic15s; each request timeout is capped by remaining deadline; sleep at most0.25s. Only print `Started server in tmux session agentchattr-server.` after readiness and only for a launch this invocation created. On failure print manual command/log/tmux hint. Compare resolved configured data_dir with status.data_dir, warn and continue on mismatch.

- [ ] **Picker red/green step.** Add failing render/select/new/archive tests, implement, then run those tests. Picker sorts descending updated_at (stable on ties), renders all agents with last_state/unread/id-known status and relative age, prints API warning. Render `fresh` when last_launch.kind is fresh and last_state is starting; add the first four hex id characters after duplicate session names. All synchronous API calls run in asyncio.to_thread.

- [ ] **Resume red/green step.** Add failing skipped-cwd/batch/refusal tests, implement, then run them. After selection print agent status and resume eligible exited agents sequentially if confirmed. Cwd defaults are local absolute paths; missing cwd is checked locally before automatic resume. Refresh workspace after successful batch.

- [ ] **Integration red/green step.** Add failing initialize/interactive selection tests, implement, then run them. Controller.initialize sets client.channel before receiver starts; plain_channel skips picker. Build PromptSession once in interactive, reuse prompt_async for picker and chat; existing direct `interactive(client)` remains plain. Main constructs controller only for new interactive session flow, validates dependencies and tty before side effects.

- [ ] **Run green:** startup/picker tests and existing prompt/reconnect integration tests; full suite.
- [ ] **Commit:** `cli: add interactive server startup and session picker`.

### Task 4: Session chat commands, workspace events, and checkpoint lifecycle

**Files:** Modify `cli_workspace_chat.py`, `cli.py`, `tests/test_cli_workspace_chat.py`.

**Interfaces:** Controller `async handle(text)->str|None`: None delegates to existing ChatClient.submit; `continue`, `quit` are consumed actions. `async close()` best-effort checkpoints selected workspace. `on_workspace(data)` consumes matching events. `async poll_forever()` checks every2s and refreshes only while an agent is starting or history_state is pending or client.websocket is None; ignores stale results after selection changes. `ChatClient.on_workspace` optional synchronous callback; event handler invokes it for workspace events. A settings callback (or equivalent pending_channel handling) reasserts the selected workspace channel on every settings event, including reconnect, even if settings omit that channel. No transport work in callback. Main/interactive own and cancel receiver and poll tasks.

- [ ] **Write command/lifecycle async tests.** Use real controller with fake API and controlled prompt. Verify exact bodies and state transitions for spawn/resume/stop/history/rename/retry/unread; tests exercise actual shlex parsing (quoted cwd/name), no API calls for invalid syntax/summary/platform failures, and real returned records. Example:

```python
controller.workspace = {'id':'ws_a','channel':'ws-a','agents':[]}
controller.prompt = AsyncMock(side_effect=['/tmp/project', 'none'])
api.action.return_value = {'agent_id':'ag_a','registry_name':'claude-1','last_state':'starting'}
await controller.handle('/spawn claude --agent-name reviewer')
self.assertEqual(api.action.call_args.kwargs['body']['history_mode'], 'none')
self.assertEqual(api.action.call_args.kwargs['body']['cwd'], '/tmp/project')
self.assertEqual(api.action.call_args.kwargs['body']['name'], 'reviewer')
controller.workspace['agents'] = [dict(api.action.return_value, provider='claude')]
await controller.handle('/resume ag_a --agent-name replacement --fresh')
self.assertEqual(api.action.call_args.kwargs['body']['name'], 'replacement')
self.assertTrue(api.action.call_args.kwargs['body']['fresh'])
```

Cover cwd default from last workspace agent then Path.cwd; trust caveat for Claude absent .claude; exact fresh/name/cwd hints; pending/done/exited outputs and wrapper log hint; /agents includes stopped workspace and non-workspace live agents; /history no args legacy display; /join/create blocked in session; plain mode rejects session-only commands; /archive no mutation on N, API archive onY then picker; /sessions checkpoints before selection; quit/EOF checkpoint once and cancel tasks; checkpoint failure warns but exit finishes; workspace event filters unrelated id and does not duplicate unchanged status lines; poll fallback and stale-result guard; reconnect refreshes selected state.

Concrete additional controller regressions (fixtures construct real ChatClient/controller and install callbacks through initialize):

```python
async def test_summary_refusal_has_exact_text(self):
    self.controller.workspace = self.ws
    await self.controller.handle('/history claude-1 summary')
    self.assertIn('summary history mode is not available in this version; use literal or none', self.output)
    self.api.action.assert_not_called()

async def test_selected_channel_survives_settings(self):
    await self.controller.initialize(AsyncMock())
    self.client.handle_event({'type': 'settings', 'data': {'channels': ['general']}})
    self.assertEqual(self.client.channel, self.ws['channel'])

async def test_pending_and_launch_failure_literals(self):
    self.controller.on_workspace(self.workspace_event(history_state='pending', last_state='starting'))
    self.assertIn('catching up…', '\n'.join(self.output))
    self.controller.on_workspace(self.workspace_event(history_state='pending', last_state='exited', last_error='boom'))
    self.assertIn(f'failed to start; see {self.data_dir}/logs/wrapper-ag_a.log', '\n'.join(self.output))

async def test_history_without_args_delegates(self):
    self.assertIsNone(await self.controller.handle('/history'))
    self.api.action.assert_not_called()
```

Use the actual workspace event envelope from app.py when implementing workspace_event fixture. Add controlled poll iterations asserting zero HTTP refresh when WebSocket exists and all agents running/done; one refresh for starting, pending, or disconnected individually. Avoid real two-second test sleeps.

- [ ] **Run red:** `python -m unittest tests.test_cli_workspace_chat -v`.
- [ ] **Command red/green step.** Add failing parsing/body/summary tests, implement shared operations, then run them. Use the literal output contract. Use shlex.split and a non-exiting ArgumentParser subclass raising CLIError instead of SystemExit; in-chat flags mirror shell flags. Resolve agents through same function as shell. Validate history/platform before mutation. Read-only presentation uses sanitized ChatClient.show; reuse session agent formatting. Store data stays server-owned.

- [ ] **Presentation/event red/green step.** Add failing status/completion/settings tests, implement, then run them. `/agents` includes each workspace agent registry_name, provider, last_state, cwd, unread_count and native_session_id presence/absence, followed by non-workspace live agents. Extend completion with /spawn,/resume,/stop,/attach,/unread,/retry,/history,/rename,/archive,/sessions, registry_names/agent_ids and provider names from configured/observed agents. `/help` explains the new commands when session mode is active. Controller.handle catches expected CLIError, prints verbatim sanitized error, and keeps prompt alive. Unexpected transport failures use sanitized stable errors.

- [ ] **Lifecycle red/green step.** Add failing checkpoint/switch/EOF tests, implement, then run them. `/quit` leaves close to finally; `/sessions` checkpoints before picker; archive endpoint owns checkpoint/stop and then picker. Reset selection on picker EOF and return quit.

- [ ] **Polling red/green step.** Add failing conditional-poll/stale-result/reconnect tests, implement, then run them. Poll tasks never prompt; they only update state and display changes. Summary failed-state interaction remains out of this slice, but display existing history_note/last_error clearly.

- [ ] **Run green:** controller and CLI tests, then full suite.
- [ ] **Commit:** `cli: wire session commands events and checkpoint lifecycle`.

### Task 5: Foreground attach with live connection and buffered output

**Files:** Modify `cli_workspace_chat.py`, `cli_workspaces.py`, `cli.py`, `tests/test_cli_workspace_chat.py`, `tests/test_cli_workspace_commands.py`.

**Interfaces:** Add shell parser `attach AGENT --session S` and reject --json; add /attach handling/completion. `tmux_target(agent)->str`; `attach_agent(agent, *, runner=subprocess.run)->int` performs foreground terminal attach after platform/session checks, never creates session. ChatClient `pause_output()` / `resume_output()` preserve sanitized output while events continue. Controller attach awaits asyncio.to_thread; prompt_async has already returned, so no prompt application needs run_in_terminal. Mid-chat /sessions uses the same pause/resume around its picker. Shell attach resolves API under request timeout, then runs foreground attach outside overall request timeout.

- [ ] **Write failing buffering/attach tests.** While a controlled attach runner blocks in an executor, inject real `ChatClient.handle_event` message/workspace events and assert cache/state update but output remains unchanged; release runner, assert buffered lines flush once and websocket task remains alive. Also test runner exception, tmux session disappeared, Windows, non-tty shell attach, unusual agent names still use stable agent_id, no new-session call, and detach return code. Buffer bound10,000 lines with a visible omission notice if exceeded (same scale as existing cached history).

Concrete attach and buffering tests:

```python
def test_buffer_keeps_cache_live_and_flushes_once(self):
    output = []
    client = ChatClient('http://127.0.0.1:8300', output=output.append)
    client.ready.set()
    client.pause_output()
    client.handle_event({'type': 'message', 'data': {'id': 7, 'channel': 'general',
                         'sender': 'human', 'text': 'during attach'}})
    self.assertIn(7, client.messages)
    self.assertEqual(output, [])
    client.resume_output()
    self.assertIn('during attach', '\n'.join(output))
    size = len(output)
    client.resume_output()
    self.assertEqual(len(output), size)

def test_missing_tmux_session_has_resume_hint(self):
    runner = Mock(return_value=subprocess.CompletedProcess([], 1))
    with self.assertRaisesRegex(CLIError, 'not running; resume with /resume claude-1'):
        attach_agent({'agent_id': 'ag_a', 'registry_name': 'claude-1'}, runner=runner)

def test_server_target_and_nested_switch(self):
    runner = Mock(return_value=subprocess.CompletedProcess([], 0))
    with patch.dict(os.environ, {'TMUX': 'inside'}):
        attach_agent({'agent_id': 'ag_a', 'tmux_session': 'server-target'}, runner=runner)
    self.assertEqual(runner.call_args.args[0], ['tmux', 'switch-client', '-t', 'server-target'])
```

Also assert controller output contains `Switch back: tmux switch-client -l`. For the async receiver-survives test, let a blocking runner signal a threading.Event on entry; while attach task is unfinished send a frame through the controlled WebSocket receiver, assert cache/state changed and receiver task is not done, release the runner in finally, then assert lines flushed once. Apply the same output pause around a controlled mid-chat /sessions picker, asserting incoming frames cannot interleave with its menu.

- [ ] **Run red:** attach-focused tests.
- [ ] **Implement terminal handoff using the literal output contract.** Keep receiver/poller on the event loop while foreground attach runs off it:

```python
client.pause_output()
try:
    await asyncio.to_thread(attach_agent, agent)
finally:
    client.resume_output()
```

`tmux_target` uses agent.tmux_session if present, otherwise derives agentchattr-<agent_id>. `attach_agent` probes `tmux has-session -t <session>` with timeout5 and captured diagnostics; absent session raises `not running; resume with ...`. Outside tmux, foreground `tmux attach -t ...` uses inherited stdio, no shell and no finite attach duration. Inside tmux (`TMUX` set), use `tmux switch-client -t <session>` and print `Switch back: tmux switch-client -l`. Preserve TMUX/TMUX_TMPDIR environment; do not kill/create any session. Error text cannot include tokens. Close/cancel cleanup always restores prompt/output. Shell attach has no WebSocket requirement, but interactive attach keeps receiver/poller running. All tests use fakes for foreground terminal takeover; actual tmux lifecycle covered separately.

- [ ] **Run green:** attach/controller/parser tests and full suite.
- [ ] **Commit:** `cli: attach to agent terminals while buffering live chat output`.

### Task 6: End-to-end session CLI, isolated auto-start, and documentation

**Files:** Create `tests/_cli_server.py`, `tests/test_cli_workspaces_integration.py`; refactor only shared fixture setup in `tests/test_cli_integration.py`; update `README.md`, `AGENTS.md`. Production changes only if a failing integration case proves a defect, reported to controller before broadening.

**Interfaces:** Shared unittest fixture owns3 temporary ports, temporary data/uploads, child server lifecycle, authenticated browser token, and command runner; no inherited test methods duplicated. Optional environment additions support PATH shim and unique TMUX_TMPDIR. Register cleanup before waits; terminate/kill then wait; never use developer tmux socket.

- [ ] **Add real integration cases.** Exact API-backed shell scenario:

```python
created = self.command('new', 'billing cli', '--json')
self.assertEqual(created.returncode, 0, created.stderr)
ws = json.loads(created.stdout)
sent = self.command('send', '--session', ws['id'], '--json', 'hello from session cli')
self.assertEqual(sent.returncode, 0, sent.stderr)
read = self.command('read', '--session', ws['id'], '--json')
self.assertIn(json.loads(sent.stdout), json.loads(read.stdout))
```

Add sessions/archived listing, exact/prefix/ambiguous resolution, non-interactive archive --yes, JSON stdout vs error stderr, summary refusal, retry/unread, unknown agents, plain-channel regression. Prompt-toolkit pipe-input tests cover empty picker creation, selected-session chat, /rename,/sessions,/quit with checkpoint effects. With tmux installed, PATH-shim kilo lifecycle through actual CLI new/spawn/stop/resume --fresh/archive; poll HTTP state and tmux disappearance, never sleep-fixed assertions. Another isolated TMUX_TMPDIR test starts a tmux server first, then invokes ensure_server with changed environment port/data overrides (passed explicitly as run.py flags), verifies actual server readiness/log hint and mismatch warning handling, then kills only isolated tmux server. Explicit --url-down test proves no tmux command is issued. Attach buffering uses Task5 controlled-terminal tests, not a paid provider.

- [ ] **Run focused integration** with temporary files only; failures must include retained log excerpt before temporary cleanup, without token URLs.
- [ ] **Document exact invocation and limits.** README examples:

```sh
python cli.py
python cli.py --channel general
python cli.py --session billing --no-resume
python cli.py new billing --json
python cli.py spawn claude --session billing --cwd /absolute/project --agent-name reviewer --history-mode literal
python cli.py resume reviewer --session billing --fresh
python cli.py attach reviewer --session billing
python cli.py unread --session billing
python cli.py archive billing --yes
```

Explain session-vs-channel flags, names/prefixes, existing --history numeric, checkpoint-on-leave/agents persist, auto-start only interactive/no--url, local URL/token model, tmux detach Ctrl+B D, trust prompt caveat, moved cwd, unknown native id, summary unavailable, Windows limitations, and server log path. AGENTS lists new CLI modules and safe tests. Note that build_release.py does not yet ship CLI/slice-1 modules (pre-existing packaging gap); do not expand this slice into a release-builder fix.

- [ ] **Run final full suite:** `python -m unittest discover -s tests -v`; `git diff --check`; confirm no isolated test children remain.
- [ ] **Commit:** `tests: verify session CLI lifecycle and document terminal workflows`.

## Self-review and coverage map

| Requirement | Task |
|---|---|
| Shared authenticated API, real response shapes, errors, name resolution | 1,2 |
| Shell commands, --session send/read, compatibility/Windows | 2,6 |
| D7 default server startup, explicit URL safety, data_dir warning/log | 3,6 |
| D15 picker, new/existing/archived, missing cwd and resume question | 3,6 |
| Workspace slash commands, live agent/history/unread/error status | 4 |
| History-mode prompt none/literal default literal; explicit summary refusal | 2,4,6 |
| Claude trust-prompt caveat when cwd/.claude absent | 4,6 |
| Slice-2 literal pending shows catching up… instead of summarizing… | 4 |
| Failed-summary [r]/[l]/[n] interaction | Deferred to slice 3 |
| /history no-arg display vs /history AGENT MODE policy | 4 |
| Selected channel survives settings/reconnect; conditional 2 s polling | 4 |
| Mid-chat picker buffers live output; nested tmux uses switch-client | 5 |
| Attach while WebSocket stays live, buffering and detach | 5 |
| Checkpoint on sessions/quit/EOF, archive server-owned shutdown | 4,6 |
| Real server, prompt input, tmux stub, safe cleanup, docs | 6 |

Shared-file interfaces: Task1 re-exports keep legacy callers stable; Task2 owns shell/parser while Task3 adds optional controller to interactive; Task4 adds controller event hook and task lifetime; Task5 adds only terminal handoff/buffering; Task6 fixture extraction preserves existing assertions. Every task's new imports/signatures are declared above. Review each task before committing the next overlapping task. Claude owns task/final reviews and new plan ledger; Codex owns implementation/fixes. Scope excludes summary generation and unrelated server-core deferred issues. Use actual API data instead of inventing missing fields.

Self-review completed against spec §5 and live API middleware: status is authenticated, so startup probes bootstrap first; token refresh invalidates cache without replaying mutations; attach is added only with working implementation in Task5; unchanged common flags avoid overload. New CLI policy/controller rulings are called out in settled behavior for Claude review.
