# Full-screen Terminal TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Claude owns task review and the ledger; Codex supplies fresh implementers and review packages. Do not dispatch worker-owned reviewers.

**Goal:** Replace scrolling interactive prompts with searchable session navigation, persistent chat/agent panes, and visible guided actions while preserving the existing shell and plain interfaces.

**Architecture:** A prompt-toolkit Application renders the authoritative ChatClient message cache and WorkspaceChatController state. Structured notifications and action outcomes separate presentation from operations; both old prompts and the TUI use shared lifecycle methods. Selection, archive, shutdown, and terminal handoff have explicit ownership boundaries.

**Tech Stack:** Python 3.11+, asyncio, prompt-toolkit >=3.0.53,<4.0, existing HTTP/WebSocket API, unittest, isolated tmux fixtures.

**Spec:** `docs/superpowers/specs/2026-09-13-terminal-tui-design.md` (user approved 2026-09-13), with parent `docs/superpowers/specs/2026-09-12-terminal-sessions-design.md` §5.

## Global Constraints

- Work on `feature/terminal-tui` in the existing checkout. Preserve `feature/terminal-sessions` at `ec8067c`. No merge/push or cleanup of shared coordination files/SDD artifacts.
- No server or slice-1 changes. No store/registry/provider-file access. Existing authenticated localhost HTTP/WebSocket paths, token secrecy, proxy bypass, and shell payloads remain binding.
- Full-screen is default for interactive tty chat; `--plain` preserves legacy rendering. Non-terminal stdin retains the existing refusal. Non-terminal stdout falls back; POSIX-only unset/empty/dumb TERM also falls back. Exactly one stderr line: `Full-screen unavailable; using plain mode.` No fallback-specific exit-code change. Windows unset TERM does not force fallback.
- Preserve `--session`, `--channel`, `--no-resume`, numeric `--history`/`--limit`, `--agent-name`, `--history-mode`, shell JSON/timeout/error contracts and helper re-exports.
- TUI state is presentation only. ChatClient owns message/connectivity state; WorkspaceChatController owns workspace selection/lifecycle. One receiver and one conditional two-second poller; no mutation replay; no lock across input dialogs.
- Notifications fire after state mutations/version bumps in the same loop tick. Preserve selection generation and same-session state revision guards across every awaited snapshot.
- Message cache stays bounded at 10,000 records. Activity is 1,000 sanitized lines with omission count. Drafts: 50 nonempty entries, 64 KiB UTF-8 each. At capacity use `50 unsent drafts; send or clear one`; never silently evict.
- Labels follow `_safe` plus zero-width/bidi-control removal. Bodies follow `terminal_text`, expanding tabs. No server-derived ANSI/HTML/style names. Display width uses `get_cwidth`.
- Wide layout >=110 columns and >=24 rows, 22-column sidebar; compact >=80 columns and >=18 rows; below that, resize/Help/Quit surface with preserved model/draft/focus.
- TUI `/sessions` opens navigation without checkpoint. Switch commit and Quit checkpoint. Completed Archive has no selection, is already checkpointed, and stops receiver/poller until the next selection. Plain-mode ordering stays unchanged.
- Quit cancels pre-commit reads/dialogs, waits shielded selection commit/mutation/attach, checkpoints, then cancels background tasks. Signals skip unsent-draft confirmation; key-driven Quit does not. Ctrl+C preserves composer text.
- No terminal repaint/exit while foreground attach owns tty. Preflight refusal never suspends. Shell `attach_agent(agent, *, runner=subprocess.run, output=print, shell_session=None) -> int` stays compatible.
- History options are literal/none, literal default. Exact refusal: `summary history mode is not available in this version; use literal or none`.
- Preserve every literal in spec §7, including `Resume N stopped agents? [Y/n]`, `Unarchive it? [y/N]`, `Archive session? [y/N]`, `fresh`, `catching up…`, `id unknown`, `⚠ cwd missing — /resume <agent> --cwd PATH`, attach/log recovery hints, and `Switch back: tmux switch-client -l`.
- Windows tmux refusal stays `Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch.` Windows full-screen is not claimed tested end to end.
- Tests use temporary ports/data/uploads, isolated TMUX_TMPDIR, no inherited TMUX, inert providers, registered cleanup, redacted logs. Never launch paid providers or kill a developer tmux socket.
- Interpreter: `/tmp/yapp-cli-venv/bin/python`. Run focused tests while iterating, then required task checks; full suite for integrated entry/final verification. Each task commits explicit files only and supplies actual concise RED/GREEN evidence.

## File boundaries and shared interfaces

| File | Responsibility |
|---|---|
| `cli_view_contracts.py` (new) | Dependency-free immutable event/result data shared by client/controller/views |
| `cli.py` | Existing transport/cache, structured submit adapter, entry selection; lazy TUI import avoids a cycle |
| `cli_workspace_chat.py` | Shared action dispatcher, selection/commit state, notifications; legacy prompt adapter remains |
| `cli_workspaces.py` | Existing API facade plus reusable attach preflight/foreground helpers |
| `cli_tui_state.py` (new) | Bounded drafts/notices and viewport anchors; no API/cache ownership |
| `cli_tui_view.py` (new) | Sanitized fragments, layouts, persistent controls, responsive visibility |
| `cli_tui_dialogs.py` (new) | Awaitable forms/confirmations/palette and contextual input |
| `cli_tui.py` (new) | Application composition, action orchestration, receiver lifetime, quit and tty handoff |
| `tests/_tui_harness.py` (new) | Real Application/controller harness with pipe input and captured screen |

These two additional small modules (`cli_view_contracts`, `cli_tui_state`) refine the spec's proposed file split: shared types must not import `cli.py`, and draft bookkeeping must not grow the renderer. No additional subsystem or state machine is introduced.

Use these exact shared value types, defined in Task 1:

```python
@dataclass(frozen=True)
class ViewEvent:
    source: str
    kind: str
    revision: int
    workspace_id: str | None = None
    agent_id: str | None = None
    message_ids: tuple = ()
    selection_generation: int | None = None
    text: str | None = None

@dataclass(frozen=True)
class ActionOutcome:
    status: str  # completed, cancelled, failed
    message: str | None = None
    workspace_id: str | None = None
    agent_id: str | None = None

@dataclass(frozen=True)
class SubmitOutcome:
    status: str  # completed, cancelled, failed
    keep_running: bool = True
    sent: bool = False  # transport accepted, not a server acknowledgment
    message: str | None = None
```

`controller.presentation` is None for legacy mode. A TUI presenter implements `confirm(text, *, default=False, escape=False) -> bool`, `attach(agent) -> ActionOutcome`, and `notice(text) -> None`; forms gather values in the application before `execute_action`. The controller never instantiates prompt-toolkit controls. Its view hook is an optional synchronous callable accepting ViewEvent. New public coroutine methods are `list_sessions(include_archived=False)`, `execute_action(action, payload)`, `dispatch_action(text)`, `select_session(ws_id)`, `cancel_selection()`, and `wait_pending()`. `bind_view(presentation, notify)` installs view mode/callbacks without starting receiver/poller. It also sets `client.on_workspace = self.on_workspace` and `client.on_settings = self.on_settings`; TUI startup never calls legacy initialize, so this binding must be self-contained. Existing initialize/handle/close APIs remain compatible.

Use existing IDs and response dictionaries without copying them into a parallel store. Every task consuming a newly introduced interface lists it below. Implementation code excerpts specify the load-bearing algorithm; integrate them with the existing surrounding validation and tests rather than pasting duplicate dispatch paths.

## Task 1: Structured client/controller notifications and submit results

**Files:** create `cli_view_contracts.py`, `tests/test_cli_view_contracts.py`; modify `cli.py`, `cli_workspace_chat.py`.

**Consumes:** existing ChatClient remember/handle_event/send/submit and controller `_select`/`on_workspace`/`on_settings`.
**Produces:** value types above; `client.on_view_change`, `client.view_revision`, `client.connection_state`, `client.submit_outcome(text) -> SubmitOutcome`; `controller.on_view_change` and `bind_view(presentation, notify)`.

- [ ] Add immutable value types and RED tests that drive real client events and inspect callback state:

```python
def test_message_event_notifies_after_cache_update_without_printing(self):
    printed, observed = [], []
    client = ChatClient('http://127.0.0.1:8300', output=printed.append)
    client.on_view_change = lambda event: observed.append(
        (event, tuple(client.messages)))
    client.ready.set()
    client.handle_event({'type': 'message', 'data': {
        'id': 1, 'channel': 'general', 'sender': 'user', 'text': 'hello'}})
    self.assertEqual(observed[-1][1], (1,))
    self.assertEqual(observed[-1][0].message_ids, (1,))
    self.assertEqual(printed, [])
```

- [ ] Run `/tmp/yapp-cli-venv/bin/python -m unittest tests.test_cli_view_contracts -v`; capture expected missing-hook/type failures before implementation.
- [ ] Implement one notification method per owner. Client increments its invalidation counter after mutations; controller emits its current `_state_revision` and `_selection_version`. Legacy output is unchanged when hook is None:

```python
def _notify_view(self, kind, *, message_ids=(), text=None):
    self.view_revision += 1
    if self.on_view_change is not None:
        self.on_view_change(ViewEvent('client', kind, self.view_revision,
                                     message_ids=tuple(message_ids), text=text))
```

Cover history completion, individual updates/deletes/clear, settings/channel rename, status/agent rename, and connecting/reconnecting/connected transitions. Full-screen show_message/history notify rather than printing cached records. Controller on_workspace emits `agent_state` instead of status lines when its hook is installed, after its existing state/version update. Notices still use client.show→output. Avoid double notifications for one state update where a called helper already notifies.

Refactor submit into a structured core, preserving legacy return meaning:

```python
async def submit(self, text):
    return (await self.submit_outcome(text)).keep_running
# Message branch inside submit_outcome:
accepted = await self.send({'type': 'message', 'text': text,
                          'channel': self.channel, 'sender': self.username})
return SubmitOutcome('completed' if accepted else 'failed', sent=accepted)
```

Keep exact validation and channel-create behavior; invalid /join or /create returns failed but legacy submit still keeps chat open. /quit returns keep_running=False. Preserve server commands, send acknowledgments in shell_command, and helper re-exports.
- [ ] Add failed-send retention signals, zero legacy-output regressions, and callback-after-controller-version tests. Run `tests.test_cli_view_contracts tests.test_cli tests.test_cli_api tests.test_cli_workspace_chat`.
- [ ] Commit these four files explicitly; report event coverage and compatibility results.

## Task 2: Shared structured lifecycle actions

**Files:** modify `cli_workspace_chat.py`; create `tests/test_cli_tui_actions.py`.
**Consumes:** ActionOutcome/ViewEvent and existing API facade/resolvers/version guards.
**Produces:** `execute_action(action, payload)`, `dispatch_action(text)`, `_action_lock`, `wait_pending()`; legacy handle delegates agent mutations to the same core.

Action allowlist/payloads: `create_session{name}`, `rename_session{name}`, `archive_session{confirmed}`, `spawn{provider,cwd,name,history_mode}`, `resume{agent_id,fresh,cwd,name}`, `stop{agent_id}`, `attach{agent_id}`, `unread{agent_id?}`, `retry{agent_id}`, `history{agent_id,mode}`. `select_session{session_id}` is wired by Task 3. Validate required fields and reject unknown action names/extra fields with CLIError before mutation. Resolve stable IDs inside the selected workspace; commands can still resolve names/provider aliases. Body keys and None values must match baseline.

- [ ] RED test shared action behavior and no replay on an overlapping event:

```python
async def test_resume_uses_one_mutation_and_returns_stable_ids(self):
    ctl = self.controller_with_workspace()
    outcome = await ctl.execute_action('resume', {
        'agent_id': 'ag_a', 'fresh': False, 'cwd': None, 'name': None})
    self.assertEqual(outcome, ActionOutcome('completed', workspace_id='ws_a', agent_id='ag_a'))
    self.api.action.assert_called_once_with('ws_a', 'resume', 'ag_a',
        body={'fresh': False, 'name': None, 'cwd': None})
```

Define `controller_with_workspace` in this test file using Mock(spec=WorkspaceAPI), a real ChatClient/controller, and a complete fixed workspace snapshot. Use threading.Event/Future barriers for event-versus-HTTP tests; do not sleep to guess scheduling.
- [ ] Run `tests.test_cli_tui_actions` RED.
- [ ] Extract shared operation work from handle without changing existing bodies or `_accept_snapshot`. Acquire the action lock only after gathering prompt values. Strongly reference and shield off-loop mutations so cancellation does not pretend they stopped:

```python
async def _run_mutation(self, function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    self._pending_mutations.add(task)
    try:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
    finally:
        self._pending_mutations.discard(task)
```

Exceptions from completed mutations must be consumed even during cancellation. Expected CLIError/OSError/TimeoutError become failed outcomes plus sanitized notices, never catch arbitrary local exceptions as transport failures. `wait_pending` awaits strong references without replay. Preserve legacy plain guard, malformed-syntax text, trust hint, fresh/name/cwd hints, and duplicate cwd/unread prevention.

`dispatch_action` parses only recognized session commands and returns None for delegated chat commands; in TUI mode it excludes /sessions,/archive,/quit,/help,/agents and no-argument /history because the application provides their view orchestration. Legacy handle still handles its original picker/close branches. `attach` invokes the injected presenter only when in TUI mode; otherwise retains the old pause/helper/resume flow. Until Task 10 wires a presenter, action tests supply an explicit fake presenter.
- [ ] Cover all action bodies, invalid IDs/summary/Windows refusal, duplicate activation serialization, failed outcome and legacy parity; run `tests.test_cli_tui_actions tests.test_cli_workspace_chat tests.test_cli_workspace_commands`.
- [ ] Commit only controller/test file; include explicit before/after API-call evidence.

## Task 3: Transactional selection, archive, and quit coordination

**Files:** modify `cli_workspace_chat.py`; create `tests/test_cli_tui_selection.py`.
**Consumes:** shared action lock/outcomes, presentation.confirm, _select/close/version guards.
**Produces:** list_sessions/select_session/cancel_selection; `_selection_task`, `_selection_commit`, `selection_pending`; archive clears TUI selection and emits a selection event. Add `select_session{session_id}` to the execute_action allowlist, validating and delegating to select_session.

- [ ] RED test cancellation before commit keeps every old-state field:

```python
async def test_cancel_dialog_preserves_old_selection(self):
    ctl = self.make_controller(old_workspace, candidate_workspace)
    ctl._closed = False
    task = asyncio.create_task(ctl.select_session('ws_b'))
    await self.dialog_open.wait()
    await ctl.cancel_selection()
    self.assertEqual((await task).status, 'cancelled')
    self.assertEqual((ctl.workspace['id'], ctl.client.channel, ctl._closed),
                     ('ws_a', 'session-a', False))
    self.api.action.assert_not_called()
```

Fixture `make_controller` installs a presenter whose confirm waits on asyncio.Event and returns the configured choice, plus a fake API containing old/candidate snapshots; no view widget needed yet. Add separate checkpoint-blocked barrier proving Quit waits for commit rather than cancelling it.
- [ ] Run selection tests RED.
- [ ] Implement fetch/dialog preparation without holding `_action_lock`. `no_resume` chooses Chat only without showing a resume prompt. Batch resume Escape is Chat only; archived Escape is No. Missing-directory agents skipped, Windows chat-only notice retained, batch resume body `{}` unchanged. Candidate API mutations use Task 2's pending tracking; completed effects are never rolled back.

```python
async def _commit_selection(self, candidate, generation):
    async with self._action_lock:
        if generation != self._selection_version:
            return ActionOutcome('cancelled')
        await self.close()  # warning-and-continue checkpoint policy
        self._select(candidate)
        self.client.channel = candidate['channel']
        self._refresh_requested = True
        return ActionOutcome('completed', workspace_id=candidate['id'])
```

Only create/shield `_selection_commit` after dialogs succeed and current generation is revalidated. First selection has no old workspace, so close makes no API call. A same-ID selection is a no-op preserving `_failed_launches`. `cancel_selection` cancels only preparation; if commit exists, await it. Convert preparation cancellation into ActionOutcome(cancelled), with stale thread reads ignored. Clear pending references in finally.

TUI archive through execute_action checks confirmation, calls server archive once, then `_select(None)` followed by `_closed=True` and a post-state selection notification. Do not call close again. Application task lifetime changes occur later in Task 10 in response to no selection. Legacy archive keeps its old behavior. No unconditional list polling.
- [ ] Cover first commit order, same-selection no-op, archive/no-checkpoint, archived decline status, batch defaults/escape, precommit API failure, stale candidate and shielded commit. Run `tests.test_cli_tui_selection tests.test_cli_tui_actions tests.test_cli_workspace_chat`.
- [ ] Commit explicit controller/selection tests. Report ordering traces and pending-task cleanup.

## Task 4: Reusable attach preflight with unchanged shell behavior

**Files:** modify `cli_workspaces.py`; create `tests/test_cli_attach_phases.py`.
**Consumes:** tmux_target, require_tmux_platform and baseline attach tests.
**Produces:** `prepare_attach(agent, *, runner=subprocess.run, shell_session=None) -> AttachTarget`; `run_attach(target, *, runner=subprocess.run, output=print) -> int`; AttachTarget has `target`, `label`, `hint`, `nested` fields. Keep attach_agent signature/default runner unchanged.

- [ ] RED tests for phase isolation:

```python
def test_preflight_only_probes_exact_target(self):
    runner = Mock(return_value=subprocess.CompletedProcess([], 0))
    prepared = prepare_attach({'agent_id': 'ag_a', 'registry_name': 'claude-1'}, runner=runner)
    self.assertEqual(prepared.target, 'yapp-ag_a')
    runner.assert_called_once_with(['tmux', 'has-session', '-t', '=yapp-ag_a'],
                                   timeout=5, capture_output=True)
```

- [ ] Run `tests.test_cli_attach_phases` RED.
- [ ] Move existing platform/TTY/target/probe validation into prepare_attach; move foreground attach/switch into run_attach. Both map OSError/SubprocessError to the exact existing fixed error. Probe output discarded; no HTTP/token access. Shell composition is:

```python
def attach_agent(agent, *, runner=subprocess.run, output=print, shell_session=None):
    target = prepare_attach(agent, runner=runner, shell_session=shell_session)
    return run_attach(target, runner=runner, output=output)
```

Run_attach uses only `runner(['tmux', 'switch-client' if target.nested else 'attach', '-t', target.target])`, without timeout/capture/stdio/env kwargs; emit the existing switch-back hint on successful nested return. No disappearance re-probe in shell facade; TUI-specific postfailure probe belongs to Task 10.
- [ ] Verify preflight failure cannot execute foreground, runner injection into both phases, return-code and hint parity. Run `tests.test_cli_attach_phases tests.test_cli_workspace_commands tests.test_cli_workspace_chat`.
- [ ] Commit helper/test pair; report exact argv checks and no shell output changes.

## Task 5: Presentation state, sanitation, and bounded drafts

**Files:** create `cli_tui_state.py`, `tests/test_cli_tui_state.py`; modify `cli_view_contracts.py` to move sanitisers unchanged; modify `cli.py`, `cli_workspaces.py`, `cli_workspace_chat.py` only to replace old definitions with imports/re-exports.
**Consumes:** existing terminal_text in cli.py and canonical _safe in cli_workspaces.py; dependency-free cli_view_contracts from Task 1; prompt-toolkit get_cwidth. Do not import cli.py into the new state module. cli_view_contracts must not import cli, cli_workspaces, cli_workspace_chat, or prompt_toolkit.
**Produces:** canonical terminal_text/_safe in cli_view_contracts with identity-preserving old-name re-exports; `label_text(value)`, `body_text(value)`, `clip_cells(text,width)`; DraftStore and NoticeStore; `layout_mode(columns,rows)`; Viewport state with anchor message ID and follow flag; `TuiState` grouping those stores plus selected-row IDs/search/focus names (presentation state only).

- [ ] RED tests cover non-destructive limits and terminal content:

```python
def test_drafts_refuse_new_destination_without_eviction(self):
    drafts = DraftStore()
    for n in range(50):
        drafts.set(('session', str(n)), 'unsent')
    self.assertFalse(drafts.can_open(('session', 'new')))
    self.assertTrue(drafts.can_open(('session', '0')))
    self.assertEqual(drafts.get(('session', '0')), 'unsent')
    self.assertEqual(drafts.capacity_notice, '50 unsent drafts; send or clear one')
```

- [ ] Run `tests.test_cli_tui_state` RED.
- [ ] Implement DraftStore with ordered entries, `get/set/clear/can_open`, UTF-8 byte validation, and cursor offsets per key; empty entries removed. `set` refuses oversize/new over-capacity edits without damaging previous text. `can_open(key, *, mandatory=False)` returns True for post-archive mandatory navigation; `set` still enforces capacity then. Store only drafts, never message snapshots.

```python
def label_text(value):
    return _safe(value)  # reuse existing policy; isprintable already removes Cf

def layout_mode(columns, rows):
    if columns < 80 or rows < 18:
        return 'small'
    return 'wide' if columns >= 110 and rows >= 24 else 'compact'
```

Ruling R-F′ (supersedes R-F): move terminal_text from cli.py and canonical _safe from cli_workspaces.py unchanged into dependency-free cli_view_contracts. Replace the byte-for-byte equivalent _safe definition in cli_workspace_chat.py with the same import. Re-export under all old names; tests assert object identity and unchanged behavior. Never copy implementations or import the old owning modules into cli_tui_state. Body policy calls terminal_text then expands tabs; labels call _safe, whose isprintable already removes directional/zero-width Cf controls. Document/test that property rather than adding a second sanitiser. Cost if wrong: three import-only edits in Task 5. clip_cells uses get_cwidth per complete character and fits ellipsis without splitting combining clusters. NoticeStore.add splits real newline-delimited lines, retains newest 1,000, tracks omitted count, and never writes stdout. Viewport stores anchor/follow/new-ID set; reconnect/update/delete adjust anchor against current client.messages without another cache. If anchor is deleted, choose the next surviving message, or the nearest previous survivor.
- [ ] Test 64-KiB UTF-8 boundary, existing drafts at capacity/postarchive composing refusal, Unicode widths/controls, 1,001-line overflow, layout breakpoints, message-ID viewport behavior. Run focused state tests.
- [ ] Run state tests plus existing client/workspace suites covering the unchanged sanitisers and new re-export identity assertions. Commit only the six listed files. Do not create widgets or API clients.

## Task 6: Awaitable dialogs and keyboard discovery

**Files:** create `cli_tui_dialogs.py`, `tests/test_cli_tui_dialogs.py`; modify `requirements-cli.txt` floor only.
**Consumes:** sanitation from Task 5; prompt-toolkit3.0.53 controls/floats/layout.
**Produces:** DialogHost(app_getter, invalidate), `confirm(text, *, default=False, escape=False)`, `form(title, fields, *, submit_label, error=None)`, `choose(title, choices, *, searchable=True)`; immutable ModalResult(value=None, cancelled=False) and Field(name,label,default='',choices=(),required=False).

- [ ] RED tests using a real Application with pipe input and DummyOutput:

```python
async def test_confirmation_y_n_default_and_escape(self):
    async with self.dialog_application() as (host, pipe):
        answer = asyncio.create_task(host.confirm('Unarchive it? [y/N]', default=False))
        await self.wait_modal(host)
        pipe.send_text('y')
        self.assertTrue(await answer)
        answer = asyncio.create_task(host.confirm('Resume 2 stopped agents? [Y/n]',
                                                   default=True, escape=False))
        await self.wait_modal(host)
        pipe.send_bytes(b'\x1b')
        self.assertFalse(await answer)
```

Define dialog_application/wait_modal in this test file using create_pipe_input, Application.run_async, a FloatContainer, and Event signaled after the host installs a dialog. Cleanup cancels/awaits the Application task.
- [ ] Run dialogs tests RED.
- [ ] DialogHost exposes `body` (an empty container while closed); Task 7 includes `Float(content=DynamicContainer(lambda: dialogs.body))` in its root FloatContainer. DialogHost owns one modal Future and saved focus. `open` installs a modal FloatContainer, focuses its first enabled control and invalidates; resolve/cancel removes it, restores valid old focus, resolves exactly once. Never block the event loop. Duplicate open requests return a clear busy/cancelled result rather than orphaning a Future.

```python
def finish(self, value):
    future, self.future = self.future, None
    self.float = None
    self.restore_focus()
    self.invalidate()
    if future is not None and not future.done():
        future.set_result(value)
```

Ruling R-G: composer escape-prefixed bindings, including ('escape', 'enter'), are filtered by composer focus. Dialog/navigation lone Escape must resolve without waiting for the composer’s 1.0-second timeoutlen; add a bounded real-key test proving dialog cancellation resolves before that delay. Cost if wrong: none.

Forms use real TextArea/RadioList/Button controls with Tab/ShiftTab, Enter submit, Esc/Ctrl+C cancel. Confirmations bind y/n and Enter default. Palette choices carry stable action IDs and display descriptions; searchable filtering preserves ID, disabled rows expose reasons. No automatic mutation from navigation/typing. Validation errors appear inside form; callers can keep current fields on failed operation. No hard-coded provider list: choices arrive from configured providers.
- [ ] Test y/n/default, focus restoration, cancellation Future cleanup, search navigation, malicious labels, no single-letter bindings in fields. Run `tests.test_cli_tui_dialogs tests.test_cli_tui_state`.
- [ ] Commit dialogs/tests/dependency floor; report headless key sequences.

## Task 7: Persistent responsive layout and real screen harness

**Files:** create `cli_tui_view.py`, `tests/_tui_harness.py`, `tests/test_cli_tui_view.py`.
**Consumes:** state stores/sanitizers, DialogHost, real client/controller and ViewEvent.
**Produces:** `TuiView(client, controller, state, dialogs, callbacks)` with `root`, `composer`, `conversation`, `navigation`, `agents`, `focus_named(name)`, `refresh(event=None)`, `screen_mode`; consumes Task 5's TuiState; `tui_harness` async context manager. `callbacks` is a mapping with async callables: `submit(text: str) -> SubmitOutcome`, `quit() -> None`, `navigate(mandatory: bool = False) -> ActionOutcome`, `run_action(action_id: str) -> ActionOutcome`, and `attach(agent_id: str) -> ActionOutcome`. Task 7 supplies explicit stubs; Tasks 8/9/10 bind these same keys and signatures to composer/workflows/application, adapting presenter attach from stable ID to the current agent record.

- [ ] RED screen assertions exercise layout, not widget construction:

```python
async def test_compact_resize_keeps_draft_and_focus(self):
    async with tui_harness(size=(120, 30)) as ui:
        ui.view.composer.text = 'draft survives'
        ui.view.focus_named('composer')
        await ui.resize(80, 24)
        self.assertEqual(ui.view.screen_mode, 'compact')
        self.assertEqual(ui.view.composer.text, 'draft survives')
        self.assertIn('F2 Sessions', ui.screen_text())
        await ui.resize(70, 16)
        self.assertIn('Resize', ui.screen_text())
        await ui.resize(120, 30)
        self.assertEqual(ui.focused_control, 'composer')
```

- [ ] Run view tests RED.
- [ ] Build controls once using HSplit/VSplit/ConditionalContainer/FloatContainer. No repeated replacing of composer buffers on notifications:

```python
main = HSplit([conversation, agent_area, composer_area])
wide = VSplit([Box(navigation, width=22), main])
root = FloatContainer(content=DynamicContainer(
    lambda: resize_notice if mode() == 'small' else wide if mode() == 'wide' else main),
    floats=[Float(content=DynamicContainer(lambda: dialogs.body))])
```

Use framed section titles, terminal-default background, cyan focus/selection, muted borders and labeled semantic status colors. Footer lists F2 Sessions/F3 Agents/F4 Commands/F1 Help/Ctrl+Q Quit. Plain-channel title says Channels and omits agent lifecycle controls. All captions/data fragments use sanitation before layout. `_agent_status` is raw until the view sanitizes it. Attachments display safe textual names/URLs; no automatic URL execution. Incoming status changes update rows, never conversation records.

Conversation renders sorted cache messages for current channel with sender/time and multiline body, existing attachments/choice text, 10,000-record bound, and viewport anchor/new counter. Derive visible rows without persistent transcript copies; retain focus and cursor. Wrap by terminal cell width; give long unbroken text bounded wrapping/clipping. Show empty/actionable screens; unknown native ID and full log/recovery strings stay reachable in inspector. Side navigation shows archived/duplicate suffixes and newest update ordering with selected full ID preserved.

Harness implementation uses mutable size and real renderer:

```python
stream = io.StringIO()
size = Size(rows=30, columns=120)
output = Vt100_Output(stream, get_size=lambda: size, enable_cpr=False)
# after_render observer:
screen = application.renderer.last_rendered_screen
rows = [''.join(screen.data_buffer[y][x].char for x in range(size.columns))
        for y in range(size.rows)]
```

`tui_harness(size=(120,30), *, client=None, controller=None)` starts a real Application with pipe input; before Task 10 it composes TuiView with explicit no-op callbacks, not a fake renderer. It exposes pipe-backed `key`, `type_text`, `resize`, `screen_text`, `focused_control`, bounded `wait_render` and `wait_until` using render Events/condition predicates, and deterministic cleanup. Later Task 10 replaces callback composition with the real TuiApplication. `key` maps F1–F4/Enter/Escape/CtrlQ/CtrlC/Tab/ShiftTab to VT sequences; help and tests explicitly account for escape timeouts.

Wire client.on_view_change to view.refresh and controller.bind_view to a test presenter delegating confirm to DialogHost and attach to an explicit controlled callback. Set client.output to the notice sink. Thus real handle_event updates trigger real renders from the first view test; no test manually copies model state into widgets.
- [ ] Test all breakpoints, sidebar divider cell, message-update/delete, wide Unicode, malicious control text, 22-column labels, disabled action reasons, no duplicate transcript output. Run `tests.test_cli_tui_view tests.test_cli_tui_dialogs tests.test_cli_tui_state`.
- [ ] Commit view/harness/tests. Save representative captured screen text in the task report; do not claim visual QA from DummyOutput.

## Task 8: Composer, contextual completion, drafts, and scrolling

**Files:** modify `cli_tui_view.py`, `cli_tui_state.py`, `tests/_tui_harness.py`; create `tests/test_cli_tui_composer.py`.
**Consumes:** persistent composer TextArea and DraftStore/Viewport; structured submit outcomes from Task 1.
**Produces:** `ComposerActions(view, state, submit, notice)` in cli_tui_view.py with `send()`, `switch_draft(key, *, mandatory=False)`, `clear_draft()`, contextual completer; key bindings and viewport navigation integrated into view. Confirmation uses `view.dialogs.confirm`.

- [ ] RED regression drives paste/new message through real cache while typing:

```python
async def test_failed_send_keeps_multiline_draft(self):
    async with tui_harness() as ui:
        ui.bind_submit(AsyncMock(return_value=SubmitOutcome('failed')))
        await ui.paste('one\ntwo')
        self.assertEqual(ui.view.composer.text, 'one\ntwo')
        await ui.key('ENTER')
        await ui.wait_until(lambda: ui.submit_mock.await_count == 1)
        self.assertEqual(ui.view.composer.text, 'one\ntwo')
```

Extend the existing harness with `bind_submit` and bracketed-paste VT sequences; these exercise actual key bindings/buffer, not direct callback invocation.
- [ ] Run composer tests RED.
- [ ] Bind Enter to accept active completion first, otherwise submit. Alt+Enter/Esc Enter inserts newline, with all escape-prefixed bindings filtered by composer focus (R-G); bracketed paste never submits. Tab cycles completion when visible, otherwise moves focus. Ctrl+C cancels overlay/completion, preserving composer. Empty Ctrl+D delegates Quit; nonempty Ctrl+D deletes forward. Clear draft confirms. Body/cursor snapshots belong to stable `(session,id)` or `(channel,name)` keys.

Capture both draft key and buffer revision during async send; clear only the sent snapshot, never newer typing or another selected draft:

```python
key, text, version = self.key, buffer.text, self.edit_version
result = await self.submit(text)
if result.status == 'completed' and key == self.key and version == self.edit_version:
    buffer.text = ''
    self.state.drafts.clear(key)
```

Prevent concurrent submit activation while one is pending; keep editing allowed. If typing changed, retain current draft after completion, with a notice that the earlier message was sent. No optimistic duplicate message record. Disconnected send fails with retained text; uncertain delivery is never replayed. Deletion of a draft after successful command must also respect edits made while dialogs were open.

Completion uses prompt-toolkit Completion(display_meta=...) with command descriptions, applicable configured providers, current full agent handles, @mentions and channels; sanitize display text and keep insertion strings unstyled. Only offer choices valid in current session/plain mode. When completion is closed, Tab remains focus navigation.

PageUp/PageDown/End affect only focused conversation. Model callbacks preserve an anchored viewport when not following; new-message indicator counts unseen new IDs, not agent unread count. Clicking/End resets follow and counter. Reconnect/history updates do not create false duplicates/new counts.
- [ ] Add named §9 item 3 regressions: `test_ctrl_c_without_overlay_preserves_composer_text` and `test_ctrl_c_closes_overlay_preserving_composer_text`, driven through real pipe input. Cover 64-KiB UTF-8 rejection, 50-draft block/mandatory archive exception, cursor preservation, y/n confirmation, cancellation, completion vs Tab focus, edited-during-send, and viewport follow. Run composer/state/view/client contract suites.
- [ ] Commit explicit four files; include tests demonstrating preserved text on failure and asynchronous update.

## Task 9: Session navigation, agent forms, and visible actions

**Files:** modify `cli_tui_dialogs.py`, `cli_tui_view.py`, `tests/_tui_harness.py`; create `tests/test_cli_tui_workflows.py`.
**Consumes:** DialogHost, controller list_sessions/select_session/execute_action, ComposerActions draft checks.
**Produces:** `TuiWorkflows(client, controller, view, dialogs, state)` in cli_tui_dialogs.py with `navigate(mandatory=False)`, `new_session()`, `agent_form(action, agent_id=None)`, `run_action(action)`, `show_palette()`; callbacks usable by the application.

- [ ] RED workflow tests exercise real controller with fake API and real pipe input:

```python
async def test_archived_only_navigation_keeps_create_and_archive_toggle(self):
    async with self.workflow_harness(active=[], archived=[archived_billing]) as ui:
        await ui.open_navigation(mandatory=True)
        self.assertIn('New session', ui.screen_text())
        self.assertIn('Show archived', ui.screen_text())
        await ui.activate_named('show_archived')
        self.assertIn('archived', ui.screen_text())
        await ui.select_row(archived_billing['id'])
        await ui.key('n')
        self.assertIsNone(ui.controller.workspace)
        ui.api.action.assert_not_called()
```

Define workflow_harness from `_tui_harness` plus TuiWorkflows callbacks and fake API data. `activate_named` reaches controls by Tab and Enter; `select_row` uses search/up/down/Enter and asserts the focused stable row ID before activation.
- [ ] Run workflows tests RED.
- [ ] Navigation fetches through controller list_sessions, filters names/IDs case-insensitively, preserves ID on refresh, toggles archived even when active list is empty. Opening alone never checkpoints. Check draft capacity before an ordinary switch, but not mandatory postarchive navigation. Cancel loading/dialogs leaves old selection/viewport/draft. First navigation Escape/CtrlC requests application Quit through callback, never legacy _pick_again.

Forms gather typed values before invoking the controller:

```python
fields = [
    Field('provider', 'Provider:', choices=tuple(controller.providers), required=True),
    Field('cwd', 'Working directory:', default=last_cwd, required=True),
    Field('name', 'Agent name:', default=''),
    Field('history_mode', 'History mode [none/literal]:', default='literal',
          choices=('none', 'literal'), required=True)]
error = None
while True:
    values = await dialogs.form('New agent', fields, submit_label='Start agent', error=error)
    if values.cancelled:
        return ActionOutcome('cancelled')
    outcome = await controller.execute_action('spawn', {
        **values.value, 'name': values.value['name'] or None})
    if outcome.status == 'completed':
        return outcome
    error = outcome.message
    fields = [dataclasses.replace(field, default=values.value[field.name]) for field in fields]
```

Define Field/ModalResult constructor behavior in Task 6; last_cwd uses reversed workspace agent cwd values then Path.cwd, matching baseline. Require an absolute directory before mutation; server remains final validator. Errors retain field values with the exact message. Reopening a failed form waits for another explicit submit; it never retries by itself. New session blank name is allowed. Rename/archive operate on stable selected ID. Resume starts ordinary; only explicit confirmed Fresh can set fresh=True. The shared controller preserves existing CLIError-status-based recovery hints; the UI displays them and exposes the ordinary directory/name/fresh controls, without reclassifying errors or assuming a nonexistent HTTP-status field on ActionOutcome. Retain underlying exact refusal plus actionable button/form.

Every palette action also has a visible control/menu. Confirm stop/archive with affected name; y/n/Enter/Escape semantics match spec. Unread/retry/history views use current server output; no new history-summary implementation. Plain-channel actions use structured client submit_outcome for /join and /create, never session APIs. Disabled Windows lifecycle controls expose exact refusal and do not launch.
- [ ] Cover create/select/rename/archive, ambiguous names but stable ID selection, batch resume defaults, moved cwd/fresh consent, history refusal, failed forms, provider choices, duplicate activation, plain channel, keyboard-only access. Run workflow/controller/dialog/composer suites.
- [ ] Commit explicit workflow/view/dialog files and tests; no main entry change yet.

## Task 10: Application lifetime, terminal handoff, and signals

**Files:** create `cli_tui.py`, `tests/test_cli_tui_application.py`; modify `_tui_harness.py`, `cli_tui_dialogs.py` only for final callback integration.
**Consumes:** TuiView/TuiWorkflows/ComposerActions, structured client/controller methods, existing resolve_session, prepare_attach/run_attach, in_terminal.
**Produces:** `TuiApplication(client, controller, *, input=None, output=None, terminal_context=in_terminal, runner=subprocess.run, initial_notices=())`; `run()`, `request_quit(signal=False)`, presenter confirm/attach/notice; `interactive_tui(client, controller, *, initial_notices=())`.

- [ ] RED tests for ownership and startup using controlled Futures/threading.Events:

```python
async def test_attach_signal_waits_for_foreground_before_restore(self):
    async with self.application_harness(block_attach=True) as ui:
        action = asyncio.create_task(ui.application.attach(self.agent))
        await ui.wait_event('foreground_started')
        quit_task = asyncio.create_task(ui.application.request_quit(signal=True))
        await ui.inject_message('still receiving')
        self.assertIn('still receiving', [m['text'] for m in ui.client.messages.values()])
        self.assertNotIn('terminal_restored', ui.events)
        ui.release_foreground.set()
        await action
        await quit_task
        self.assertLess(ui.events.index('foreground_finished'), ui.events.index('terminal_restored'))
        self.assertLess(ui.events.index('checkpoint'), ui.events.index('receiver_cancel'))
```

Use real receive_forever with the existing queue-backed fake WebSocket connector for this test. Inject messages through its receive queue; direct handle_event is reserved for non-transport view cases. Terminal context records enter/exit; runner blocks only foreground argv and returns controlled codes.
- [ ] Run application tests RED.
- [ ] Compose Application and callbacks; bind controller view before selection, client.output=thread-safe notice sink, client.on_view_change refresh callback. No print/patch_stdout rendering inside TUI. Start Application first with mandatory navigation if no selector, then background receiver/poller only after first selection/channel assignment. Plain-channel starts receiver after assigning channel. `run` returns without background tasks on initial Escape/CtrlC/CtrlQ. API status/data_dir startup remains supplied by cli main. For explicit `controller.selector`, TuiApplication.run resolves it inside the running Application: `await controller.list_sessions(include_archived=True)` then existing `resolve_session`, then transactional select_session. Missing/ambiguous selector CLIError propagates; an explicit archived decline raises existing `CLIError('Archived session was not selected')`. Mandatory navigation cancellation without an explicit selector returns normally. `interactive_tui` returns None on normal Quit/initial cancellation and propagates CLIError from run after cleanup to main’s existing `except ValueError` exit-1 path; never swallow it in a detached startup task.

Keep one task registry. Selection notifications reconcile desired task lifetime; successful archive cancels/awaits both tasks and opens mandatory navigation, then next commit restarts one of each. Ordinary navigation/forms never restart tasks. Preserve client/controller callback ownership; no duplicate state observers overriding each other.

TUI command dispatch handles /help,/agents,/sessions,/quit,/exit and no-arg /history as presentation actions; /archive confirms then executes action and mandatory navigation; other session commands use dispatch_action; remaining chat/server commands use submit_outcome. Return SubmitOutcome so composer knows whether to clear. Plain /history arguments preserve legacy delegation. Failed/cancelled actions retain the draft.

Attach preflight runs before suspension; foreground ownership algorithm:

```python
async with self.handoff_lock:
    prepared = await asyncio.to_thread(prepare_attach, agent, runner=self.runner)
    if self.quitting:
        return ActionOutcome('cancelled')
    async with self.terminal_context():
        worker = asyncio.create_task(asyncio.to_thread(
            run_attach, prepared, runner=self.runner, output=self.notice))
        self.attach_task = worker
        try:
            try:
                code = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await asyncio.shield(worker)
                raise
        finally:
            self.attach_task = None
```

Put that owned sequence in `_attach_owned(agent)`. Public `attach(agent)` creates and stores `self.handoff_task = asyncio.create_task(self._attach_owned(agent))` before its first await; await it with the same shield-and-wait cancellation discipline. Quit awaits handoff_task, so it cannot race between preflight and lock acquisition. UI action serialization prevents duplicate attach. Ruling R-H: add a pipe-input Application test using real `in_terminal()` alongside the controlled terminal_context tests, proving ownership/restore ordering with the actual API. Cost if wrong: one extra test. On nonzero foreground return, re-probe exact target only in TUI adapter (may reuse prepare_attach, whose missing-target CLIError is exactly `not running; resume with /resume <agent>`); gone→existing resume hint, otherwise fixed failed-status notice. OSError/SubprocessError never expose diagnostics. Never call pause_output/resume_output in TUI. Ensure expected preflight CLIError maps to a notice/outcome without entering terminal_context.

request_quit is idempotent. For key Quit, confirm any unsent draft; signal Quit skips confirmation. Cancel selection preparation and await commit/pending mutations/attach, then close checkpoint, cancel/await receiver/poller, cancel modal Futures, exit Application. Never hold an action lock while awaiting a user dialog. Handle `Keys.SIGINT` separately from `c-c`, register SIGTERM through event-loop-safe scheduling, restore previous handlers on exit. During handoff keep signal handling live even when Application input is detached; external signals schedule request_quit, not immediate Application cancellation. If the event-loop platform cannot register add_signal_handler, install a main-thread signal.signal callback forwarding with call_soon_threadsafe and restore it in finally. SIGKILL is not catchable.
- [ ] Cover startup no-task exits, initial selector decline exit1, postarchive mandatory task stop/restart, precommit/commit Quit, preflight refusal/no suspend, nested switch, missing target race, shield cancellation, signal bypass confirmation, output-to-notice, exception restoration. Run application plus all TUI-focused suites.
- [ ] Commit application/harness/callback integration explicitly. Report exact task/tty ordering and limitations.

## Task 11: Default entry, fallback, legacy migration, and docs

**Files:** modify `cli.py`, `README.md`, `AGENTS.md`; create `tests/test_cli_tui_entry.py`; adjust existing CLI integration tests only to explicitly choose legacy paths where that is their purpose.
**Consumes:** interactive_tui, unchanged interactive legacy function, startup ensure_server and all shell parsers.
**Produces:** default TUI entry, interactive-only --plain, capability fallback and documentation.

- [ ] RED offline parser/dispatch matrix:

```python
def test_unset_term_falls_back_only_on_posix(self):
    self.assertEqual(choose_interactive_mode(plain=False, stdin_tty=True,
        stdout_tty=True, platform='linux', term=None), 'plain')
    self.assertEqual(choose_interactive_mode(plain=False, stdin_tty=True,
        stdout_tty=True, platform='win32', term=None), 'tui')
```

Define `choose_interactive_mode(*, plain, stdin_tty, stdout_tty, platform, term)` in cli.py as a pure decision helper. Keep main’s existing `parser.exit(1, "Interactive chat requires a terminal...")` refusal unchanged, before config/HTTP/process and before calling choose_interactive_mode. Do not convert it to CLIError; preserve `test_non_tty_rejects_before_config_http_or_process`. The helper receives valid stdin after this guard. Main prints fallback line only for automatic plain selection, not explicit --plain.
- [ ] Run entry tests RED.
- [ ] Register --plain top-level and chat only, default False; reject its use with non-chat commands before config/network access, including before-shell placement. Keep JSON availability list and existing shell dependency gate. Lazy import cli_tui only for chosen full-screen mode.

```python
mode = choose_interactive_mode(plain=args.plain, stdin_tty=sys.stdin.isatty(),
    stdout_tty=sys.stdout.isatty(), platform=sys.platform, term=os.environ.get('TERM'))
if mode == 'plain' and not args.plain:
    print('Full-screen unavailable; using plain mode.', file=sys.stderr)
```

Run ensure_server before screen startup, collecting sanitized startup output into initial_notices while still printing it at its existing severity/destination. Preserve conditional tmux-session hint, explicit URL no-auto-start, data_dir warnings, Windows/refusal ordering. Then call legacy interactive or interactive_tui with the existing client/controller. Explicit selector missing/ambiguous errors and archived decline propagate as CLIError from interactive_tui to main’s existing except ValueError path and exit1; normal initial cancel exits normally.

README shows default launch, --plain fallback, keymap, visible actions, draft limits and nonpersistent drafts, attach/detach, no summary, platform limits. AGENTS documents new module boundaries, headless screen capture and isolated PTY QA. Existing README parser guard remains. Keep shell examples unchanged. In `tests.test_cli_workspace_chat.MainIntegrationTests`, `test_default_chat_probes_status_and_passes_picker_controller`, `test_explicit_session_and_no_resume_are_carried_to_controller`, and `test_explicit_channel_is_plain_but_still_checks_server` keep their legacy interactive assertions under redirected non-tty stdout and now assert exactly one fallback stderr line. Add separate patched-tty/default-TUI coverage. Startup-failure tests retain their existing stderr hints plus the single automatic fallback notice when capability selection precedes startup; shell and non-tty-stdin tests get no fallback line (R-H).
- [ ] Test both flag positions, shell rejection, tty/TERM/platform matrix, exact one stderr notice, existing shell JSON/stdout, startup failures, --channel and --session paths. Run full suite `/tmp/yapp-cli-venv/bin/python -m unittest discover -s tests -v`, diffcheck and isolated-child inspection.
- [ ] Commit explicit entry/docs/tests. This is the first default-entry integration; no release-builder change.

## Task 12: End-to-end terminal UX and final acceptance

**Files:** create `tests/test_cli_tui_integration.py`; modify `tests/_tui_harness.py`, `README.md`/`AGENTS.md` only for verified corrections.
**Consumes:** completed default TUI and existing `_cli_server.py` isolated server fixture; real tty/pipe harness.
**Produces:** reproducible end-to-end/PTY QA evidence and final user-facing guide.

- [ ] Add a real-server regression using Application/controller/key sequences, not private UI state assignments:

```python
async def test_real_session_message_switch_and_quit(self):
    async with self.real_tui() as ui:
        await ui.activate_named('new_session')
        await ui.fill_field('name', 'billing')
        await ui.activate_named('submit')
        await ui.wait_selected('billing')
        await ui.type_text('hello from TUI')
        await ui.key('ENTER')
        await ui.wait_message('hello from TUI')
        await ui.key('F2')
        await ui.activate_named('new_session')
        await ui.fill_field('name', 'frontend')
        await ui.activate_named('submit')
        await ui.wait_selected('frontend')
        await ui.key('CTRL_Q')
        await ui.wait_closed()
        self.assertEqual(ui.server_message_count('hello from TUI'), 1)
        self.assertEqual(ui.checkpointed_session_names, ['billing', 'frontend'])
```

Use `class TuiIntegrationTests(IsolatedCliServer, unittest.IsolatedAsyncioTestCase)` for this async test, or a synchronous def test method calling asyncio.run(scenario()). IsolatedCliServer alone inherits plain unittest.TestCase and never awaits async test methods; that shape is forbidden. Define real_tui using isolated fixture ports/data and real ChatClient/WorkspaceAPI/controller/TuiApplication. Record checkpoint calls with a wrapper that calls the real HTTP API, not a substitute. Server message count uses authenticated API. All wait helpers use bounded events/readiness predicates. Complete spec §9 scenario with `client.handle_event` workspace injection; end-to-end message reception itself comes over real WebSocket.
- [ ] Run focused TUI integration tests; diagnose any failure as test-contract versus production defect before editing. Post proved production defects to Claude before expanding this task's source scope.
- [ ] Add isolated PTY smoke capturing actual terminal output at 120×30, 80×24, 70×16, then restore wide. Feed bracketed paste, navigation, cancellation and Quit. For actual tmux attach smoke, create inert temporary agent session on an isolated socket, exercise outside and nested switch-client, detach/return, and confirm redraw plus draft survival. Never reuse default TMUX socket. Register process/socket cleanup before launch. Do not label fake runner checks as real terminal QA.

```python
env = isolated_environment()
env.pop('TMUX', None)
env['TERM'] = 'xterm-256color'
# PTY child uses the fixture's three ports/data/uploads and this environment.
# Create only an inert sleep/read-loop agent under the isolated tmux server.
```

Use fixture isolated_environment as defined in existing integration tests; if extracting it for reuse, preserve its semantics and tests. Mouse events may be scripted through VT sequences, but visually inspect resulting screen captures. For bitmap review, render captured terminal cells using a local fixed-width font into an artifact; do not generate a decorative mockup and call it runtime evidence. Record keyboard-only, mouse, copy/selection, errors, resizing and terminal-restoration cases actually exercised; explicitly record any unavailable manual case.
- [ ] Verify docs match actual controls and defaults. Run full suite once on final code, `git diff --check`, check isolated child cleanup. Preserve reports and sanitized screenshot/text artifacts in this TUI SDD workspace.
- [ ] Commit explicit integration/doc changes. Send Task 12 review package, then whole-TUI review from the plan/spec baseline with D16–D28 and all deferred findings. One final fix worker, one scoped re-review per established SDD workflow; do not merge or push without user instruction.

## Spec coverage and review gates

| Requirement / decisions | Tasks |
|---|---|
| D16 default full-screen / shared operations | 1–3,7–11 |
| D17/D24 prompt-toolkit floor | 6,7,10,11 |
| D18/D23/D28 plain/tty/POSIX/Windows compatibility | 2,4,9,11,12 |
| D19 responsive screen / persistent input | 5,7,8,12 |
| D20 literal and presentation amendments | 1,3,7,9–12 |
| D21 branch preservation | Every task and final integration decision |
| D22 transactional navigation/checkpoint | 3,9,10,12 |
| D25 draft capacity/non-destructive behavior | 5,8–10,12 |
| D26 completed archive and mandatory navigation | 2,3,9,10,12 |
| D27 signals, preparation cancellation, shielded commit | 2,3,10,12 |
| Spec §7 exact literal placement | 3,4,7,9,10,11 plus literal table below |
| Security, bounded state, no mutation replay | 1–5,7–12 |
| Real screen/tty evidence and platform limits | 7,10,12 |

Literal verification ownership:

| Literal family | Owner and assertion |
|---|---|
| Sessions/New session/Show archived/Session name | Tasks 7/9 screen and form labels; archived-only integration |
| fresh/catching up…/id unknown/cwd missing/failure-log hint | Tasks 7/9 rendered row+inspector text, sanitized raw _agent_status |
| Resume N…[Y/n]/Unarchive…[y/N]/Archive session…[y/N] | Tasks 3/6/9 exact dialog body and y/n/Enter/Escape behavior |
| Working directory/History mode/summary refusal | Tasks 2/9 field labels and failed-action notice |
| Windows refusal/not running/Could not attach/Switch back | Tasks 4/10 preflight+foreground tests, Task 12 tty smoke |
| Started server/Start it manually/Server log/conditional Tmux session | Task 11 pre-screen stdout/stderr and exit assertions |
| Full-screen fallback/50 unsent drafts | Tasks 5/8/11 exact error strings and no unwanted side effects |

Before dispatch, controller records task self-consistency and all shared-file/interface edges in `preflight-scan.md`; Claude owns ledger rulings. Task review is scoped to recorded base..head with brief/report/diff; overlapping source edits wait for clearance. Disjoint read-only preparation may proceed. After each task: inspect concise evidence, package exact range, append milestone to AGENT_MESSAGES.md, and read all newly appended Claude entries. Never infer a gate from a stale handover or timestamp.

### Pre-flight self-consistency scan

| Task | Tests versus implementation/files/interfaces checked |
|---|---|
| 1 | Real event test observes cache-after-mutation; hooks avoid transcript printing. SubmitOutcome retains legacy keep-running bool. New types are dependency-free, preventing cli/controller cycles. |
| 2 | Resume body test matches baseline nullable fields. ActionOutcome has no HTTP-status field: controller retains status-based hints, UI does not assume that field. Select-session action is introduced only by Task 3. |
| 3 | Preparation cancel returns cancelled and leaves old state; shielded commit calls close only after dialogs. Archive emits final no-selection/closed state before UI task changes. |
| 4 | Preflight test expects exact probe only; run_attach owns inherited-stdio foreground. Shell facade composes both without TUI-only re-probe. |
| 5 | Canonical sanitisers move unchanged into dependency-free contracts with old-name identity tests; no forbidden cli import. TuiState is created here, not ambiguously by Task 7. Limit tests preserve old drafts; mandatory navigation bypass does not bypass composing limits. |
| 6 | Form API includes error text and immutable Field defaults, enabling Task 9 failure-retention loop. Confirm Escape result and y/n bindings match caller-specified defaults. |
| 7 | Harness captures real renderer cells and owns its declared file. Hooks are wired to refresh; same controls survive resize rather than reconstructed buffers. |
| 8 | Harness modifications are explicitly included. Async send captures key plus edit revision, so successful completion cannot clear newer text. Clear confirmation uses view.dialogs. |
| 9 | Workflow class location is cli_tui_dialogs; harness changes declared. Failure loop waits for explicit form resubmission and never automatically retries. Typed payloads match Task 2. |
| 10 | Preflight sits inside owned handoff sequence but outside terminal suspension. Public handoff_task is registered before await; Quit cannot race its creation. Archive task lifecycle is separate from ordinary navigation. |
| 11 | Pure capability helper handles POSIX/Windows distinction and stdin refusal before fallback; --plain top-level/shell rejection prevents ignored flags. Startup remains before screen entry. |
| 12 | Real integration wraps actual checkpoint HTTP calls; PTY smoke differs from fake-runner unit evidence. Production fixes require proved defect and scoped authorization before expansion. |

### Shared-file and direct-interface pair scan

All listed overlaps are sequential review gates, not parallel edit opportunities. Values/interfaces named here match their producing task; fixes discovered during drafting are already reflected above.

| Task pair | Shared file/interface | Reconciliation |
|---|---|---|
| 1–2 | cli_workspace_chat; ActionOutcome and controller hooks | Task 2 consumes immutable outcomes/hooks; legacy return adapter stays separate. |
| 1–3 | cli_workspace_chat; versioned notifications | Selection notification follows _select/version bump; no precommit view mutation. |
| 1–7 | client/controller hooks and ViewEvent | View reads authoritative models after notification; harness registers callbacks once. |
| 1–8 | SubmitOutcome | Composer clears only completed result and unchanged draft revision, not keep_running alone. |
| 1–9 | submit_outcome for plain channel actions | Workflows call structured client adapter; no duplicate channel API. |
| 1–10 | transport hooks/types | Application owns callback installation and task lifetime; lazy imports avoid circular dependency. |
| 1–11 | cli.py | Entry change preserves newly factored core and all helper re-exports. |
| 1–12 | real client.handle_event/receive_forever | Integration injects events via protocol, never view state. |
| 2–3 | cli_workspace_chat; action lock/pending tasks | No action lock across dialogs; selection commit owns lock and shield. |
| 2–9 | execute_action/dispatch_action | Stable payload fields, nullable keys, failures/hints remain shared; forms never form shell strings. |
| 2–10 | dispatch_action/wait_pending | TUI intercepts presentation-only commands; pending mutation wait occurs before teardown. |
| 3–9 | list_sessions/select_session | Navigation does not close; commit/cancellation owns state. Draft check happens before ordinary switch. |
| 3–10 | selection events/cancel_selection | Initial/archive no-selection governs receiver/poller; Quit cancels preparation but awaits commit. |
| 3–12 | checkpoints/select/archive | Real HTTP recorder proves one checkpoint per departed active session and no postarchive duplicate. |
| 4–10 | prepare_attach/run_attach | Preflight before suspension, strong foreground task, TUI-only exact failure re-probe. |
| 1–5 | cli.py/cli_workspace_chat/cli_view_contracts | Task 5 moves only canonical sanitizer definitions into contracts and preserves re-exports; types and event/submit behavior stay unchanged. |
| 2–5 | cli_workspace_chat | Task 5 replaces duplicate _safe with canonical import; shared actions remain unchanged. |
| 3–5 | cli_workspace_chat | Sanitizer import-only edit preserves transactional selection and notification behavior. |
| 4–5 | cli_workspaces | Canonical _safe moves unchanged with re-export; attach phases and shell contracts stay unchanged. |
| 5–6 | label sanitation | Dialog labels cannot become control sequences/markup; fields retain actual values for validation. |
| 5–7 | TuiState/Viewport/layout_mode | 22-column wide sidebar and exact breakpoints; no message cache in state. |
| 5–8 | cli_tui_state; DraftStore/Viewport | Preserve limits while adding revision/cursor behavior; no silent eviction on send/resize. |
| 5–9 | draft admission | Mandatory archive navigation may proceed; new draft text still respects capacity. |
| 5–10 | notice sink/drafts | Worker-thread text marshaled onto loop; Quit reads all unsent drafts. |
| 6–7 | DialogHost/root FloatContainer | View supplies modal float and focus targets; host restores valid focus. |
| 6–8 | confirm/defaults | Clear draft uses explicit confirmation; Ctrl+C itself never deletes. |
| 6–9 | cli_tui_dialogs; Field/ModalResult | Frozen fields updated with dataclasses.replace when retrying a user-submitted failed form. |
| 6–10 | cli_tui_dialogs; awaitable presenter | Application delegates confirm to host; callback integration does not duplicate forms. |
| 6–11 | prompt-toolkit floor | Default entry requires installed declared CLI dependencies; shell lazy import remains. |
| 7–8 | cli_tui_view and tests/_tui_harness | Composer extends persistent controls and harness key operations; no buffer rebuilding. |
| 7–9 | cli_tui_view and tests/_tui_harness | Callbacks gain real workflow behavior; stable row IDs/search/render contract retained. |
| 7–10 | TuiView and tests/_tui_harness | Real TuiApplication replaces test composition while preserving captured-screen observer. |
| 7–12 | tests/_tui_harness | PTY tests supplement, not substitute, headless screen assertions. |
| 8–9 | cli_tui_view/harness; ComposerActions | Workflows consult draft admission before select and restore per-key input after commit. |
| 8–10 | tests/_tui_harness; ComposerActions | Application supplies structured submit/quit callbacks; no loss of edited-during-await text. |
| 8–12 | tests/_tui_harness | Paste/key helpers stay real VT input; real-server acceptance checks no duplicate messages. |
| 9–10 | cli_tui_dialogs/harness; TuiWorkflows | Application installs complete callbacks and archive lifecycle; no legacy picker branches. |
| 9–12 | tests/_tui_harness; navigation/form key helpers | Integration reaches controls by keyboard and verifies stable IDs before activation. |
| 10–11 | interactive_tui/TuiApplication | CLI lazily imports new entry and passes startup notices; legacy interactive remains callable. |
| 10–12 | tests/_tui_harness; real application | Final test uses actual background ownership/signal paths; no replacement state machine. |
| 11–12 | README/AGENTS and CLI entry | Final docs corrections follow real UI evidence, preserve shell examples and parser guard. |

Pre-flight decisions to record in the TUI ledger: shared data types get a dependency-free module; draft/viewport state gets a presentation-only module; Task 8/9 harness edits are explicit; structured outcomes retain the spec's fields, and controller-owned recovery hints prevent invented HTTP-status fields. Cost: two small modules and additional harness integration, with no new public server API or user-facing scope.

All 12 tasks are one approved delivery, not an invitation to stop after the first screen. Runtime quality requires real rendering/interaction evidence and the final review, not only passing model tests.
