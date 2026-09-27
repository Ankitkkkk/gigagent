# Full-screen terminal UX

Date: 2026-09-13
Status: Technical design approved at `fb4b68a`; final review rulings incorporated. User approved the written design on 2026-09-13. Implementation plan review is next.
Branch: `feature/terminal-tui`, based on reviewed CLI head `ec8067c`.

## 1. Outcome and scope

Make everyday session work discoverable without remembering slash commands. The user can find a session, read its conversation, see agent state, write a message, and launch or attach to an agent from one persistent terminal screen.

The first version includes searchable session navigation, a scrollable conversation, a multiline composer, agent status and actions, guided forms, a command palette, and keyboard help. It reuses the authenticated HTTP/WebSocket interfaces and existing lifecycle controller. No server or slice-1 change is required: all inspector fields come from existing endpoints. Shell commands retain their output, arguments, and scripting behavior.

This is the TUI slice. It does not implement the separately deferred summary-history slice, provider-terminal emulation, file editing, attachment uploading, or release packaging. Existing server commands remain accessible through the composer.

## 2. Entry points and compatibility

- `python cli.py` and `python cli.py chat` open the full-screen UI by default.
- `--session NAME` opens that session and keeps the existing stopped-agent resume decision. `--no-resume` suppresses that decision.
- `--channel NAME` opens full-screen plain-channel chat. The sidebar lists channels; session-only actions are absent. Existing channel commands continue to work.
- A new interactive-only `--plain` flag runs the existing line-oriented interface. It works on the bare invocation and the explicit `chat` command. Passing it with a shell command is rejected before configuration or network work.
- Full-screen mode requires terminal stdin and stdout. With terminal stdin but non-terminal stdout, automatically use plain mode and emit exactly one stderr line: `Full-screen unavailable; using plain mode.` On POSIX only, unset/empty/`dumb` `TERM` also triggers that fallback. Windows does not require `TERM` and gets full-screen chat with existing tmux-operation refusals. Fallback alone does not change the exit code. Non-terminal stdin retains the existing refusal directing scripts to `read` or `send`; this validation precedes fallback. Explicit `--plain` skips capability selection without printing a fallback notice. Screen-reader users and Windows users needing compatibility can choose it explicitly.
- Existing auto-start rules remain: interactive local chat only, never auto-start with explicit `--url`, no server restart or duplicate-session replacement. Startup diagnostics appear before screen entry and remain available in the UI's activity panel.
- Exiting checkpoints the selected session and leaves server and agents running.
- Unexpected local full-screen failures restore terminal ownership and exit 1 with exactly `Full-screen terminal stopped after an unexpected local error (TYPE); rerun with --plain.` The type name replaces `TYPE`; exception messages and authenticated URLs are never included. This R-T11c mapping applies to unexpected exceptions in the full-screen branch, including its lazy import. Existing `CLIError`/`ValueError` and interrupt handling stay unchanged. No runtime retry into plain mode occurs.

## 3. Layout and visual language

Use the terminal's normal background and foreground, one cyan accent for selection/focus, muted separators, and semantic success/warning/error accents. State always has a readable label; color and symbols are supplementary. Never render server-supplied text as ANSI or markup. Message bodies use existing `terminal_text` semantics, preserving newlines and expanding retained tabs to spaces. Single-line labels use the `_safe` policy: remove tabs/newlines, terminal controls, and zero-width/bidi formatting controls. Measure/truncate sanitized fragments with `prompt_toolkit.utils.get_cwidth`, never Python string length. Style names come only from application code.

Wide layout, at least 110 columns and 24 rows:

```text
 yapp    billing                         Connected
┌ Sessions ──────────┬ Conversation ──────────────────────┐
│ Search sessions…   │ 10:42  you                          │
│                    │ Review the payment retry behavior. │
│ > billing          │                                     │
│   frontend         │ 10:43  claude-1                     │
│   migration        │ I found two retry paths…            │
│                    │                                     │
│ [New session]      │                          3 new ↓    │
│ [Show archived]    ├ Agents ─────────────────────────────┤
│                    │ claude-1  running   unread 0         │
│                    │ codex-1   exited    unread 3         │
│                    │ [New agent] [Attach] [More actions] │
│                    ├ Message ────────────────────────────┤
│                    │ Write a message or /command…       │
└────────────────────┴─────────────────────────────────────┘
 F2 Sessions   F3 Agents   F4 Commands   F5 Activity   F1 Help   Ctrl+Q Quit
```

The left sidebar occupies 22 columns including its borders, matching the schematic. The main pane holds the conversation, a compact agent area, and a composer that grows from three to six lines. An expanded agent inspector shows provider, directory, unread count, history state, resume availability, and relevant recovery guidance. Long labels truncate by display width; focusing a row exposes its full label in the inspector/help area.

At 80–109 columns or 18–23 rows, navigation becomes an overlay opened with F2, and the agent area becomes a compact status row opened with F3. Conversation and composer retain the usable space. Below 80 columns or 18 rows, show a compact resize message plus Help and Quit; keep connection state, draft, and background work intact. Resizing back restores the focused control without losing input.

Empty states are actionable: “No sessions yet” with New session and Show archived; “No agents in this session” with New agent; “No messages yet” with composer guidance. An empty active-session list must not hide archived sessions.

## 4. Navigation and message composition

| Input | Behavior |
|---|---|
| F1 | Help overlay with currently available actions and shortcuts |
| F2 | Focus/open session or channel navigation and search |
| F3 | Focus/open agents and the selected agent's actions |
| F4 | Open searchable command palette |
| F5 | Open/toggle persistent Activity diagnostics; Escape returns to prior focus |
| Tab / Shift+Tab | Move focus between controls; accept/cycle an open completion menu first |
| Up / Down | Move the focused list or completion selection; normal cursor movement in the composer |
| Enter | Activate a selected action; in composer, send or execute the current input |
| Alt+Enter, also Esc then Enter | Insert a composer newline across terminals without distinct Shift+Enter support |
| PageUp / PageDown | Scroll conversation when its pane has focus |
| End | Jump to the latest message when conversation has focus |
| Escape | Dismiss the current overlay or completion, then restore prior focus |
| Ctrl+C | Cancel the current form/overlay/completion; preserve composer text when nothing is open; never stop agents |
| Ctrl+Q | Quit, confirming first if any unsent draft exists |
| Ctrl+D | In an empty composer, request the same draft-aware Quit confirmation as Ctrl+Q; otherwise normal forward-delete behavior |

Mouse selection, buttons, and wheel scrolling are supported, but every operation is keyboard-accessible. Buttons have full text labels. Single-letter actions never capture typing in text fields.

The composer supports multiline paste and suggestions for commands, agent mentions, and applicable arguments. Pasting never submits automatically. Enter accepts a highlighted completion before sending. A visible hint explains Enter and Alt+Enter when the composer has focus. Esc then Enter depends on prompt-toolkit's terminal escape flush timeout (`Application.ttimeoutlen`) and key-sequence timeout (`Application.timeoutlen`); a lone Escape may wait for those ambiguity windows, so help documents the timing. Clear draft is an explicit palette action with confirmation; Ctrl+C never silently clears text.

Preserve one in-memory draft per visited session/channel, bounded to 50 drafts of at most 64 KiB of UTF-8 text each. Empty drafts do not consume a slot. When all 50 slots contain unsent text, refuse switching to an uncached destination with `50 unsent drafts; send or clear one` and retain the current selection and text. No eviction dialog or silent eviction. Revisiting an existing draft stays possible. Drafts are not written to provider files or persisted to disk. Cap composer input at 64 KiB with an explanatory message.

Do not clear the composer when disconnected, when a command fails, or when a form is cancelled. Clear a message after the existing transport accepts the send; display the server echo as the conversation record. Do not describe socket acceptance as server acknowledgment. Transport failure/uncertainty retains the draft and explains that retry is manual. There is no automatic message replay.

When the user is at the bottom, follow new messages. When they scroll away, preserve the viewport and show a local “N new” affordance that jumps to the bottom. This counter is distinct from the server's per-agent unread count. Updates, deletions, and reconnect history reconcile by message ID and do not duplicate displayed messages.

## 5. Session and agent workflows

Session search is a case-insensitive local filter over fetched names and IDs. Rows are keyed by full ID, not display names. Preserve selected ID across refresh/reordering. Use existing newest-updated ordering and duplicate-name ID suffixes. Show archived state explicitly. Refresh on entry, explicit Refresh, completed session changes, and reconnect; do not introduce unconditional background list polling.

Full-screen startup without `--session` opens mandatory session navigation before starting the receiver or poller. Escape/Ctrl+C/Ctrl+Q at this initial navigation exits without checkpointing or starting those tasks. With `--session`, resolve the selector then enter the same selection path. Only after the first successful selection commit and channel assignment do receiver and poller start. Plain-channel startup assigns the requested channel first and starts only the receiver; legacy plain-mode startup order stays unchanged.

`controller.select_session(ws_id)` is the full-screen switch authority. If the ID is already selected, return a completed no-op without checkpointing, prompting, or clearing `_failed_launches`. Otherwise capture the old selection generation; fetch the candidate workspace; run archived and stopped-agent dialogs; fetch updated candidate state if its actions changed it; verify the captured generation still matches; enter the serialized, shielded commit section: checkpoint old → `_select(new)` → `client.channel` → reconciliation request. No action lock is held across user-input dialogs. A selection-in-progress flag disables competing UI actions, while the generation check handles asynchronous invalidation. `_select` resets `_closed` for the new selection and increments the generation before notifications. The checkpoint uses the existing warning-and-continue policy; commit follows it without another cancellable prompt. Before commit, loading failure or cancellation leaves the old selection, `_closed`, channel, draft, and viewport intact. Do not call `close()` while merely opening navigation or fetching candidates, and never un-close an abandoned old selection. Candidate API actions already completed (such as an explicit Unarchive) are not rolled back or replayed.

Quit during pre-commit fetch/dialog work cancels that preparation with a cancelled `ActionOutcome`, leaves the old selection intact, and then follows the normal Quit path. A cancelled off-loop read may finish in its worker, but its result is ignored. Once commit begins, Quit waits for its shielded checkpoint/select/channel/reconcile sequence before `close()`; it never observes half a selection. Expected wait is bounded by the checkpoint API's `--timeout`. Already-running mutation threads are awaited for a definite outcome and never automatically replayed.

Disable sending and competing lifecycle actions during a switch. Reject stale selections before checkpoint/commit. F2 and `/sessions` both open navigation without checkpointing in TUI mode; checkpoint occurs on a committed switch or Quit. `--plain` keeps its existing `/sessions` checkpoint-then-picker sequence.

For a stopped-agent batch, display the exact question `Resume N stopped agents? [Y/n]` with Resume and Chat only choices. Initial default is Resume; Escape means Chat only and proceeds into the selected session without launches. `--no-resume` means Chat only without prompting. Missing-directory agents are explained and excluded as today. On Windows, show the existing tmux limitation and enter chat without launching.

Selecting an archived session opens `Unarchive it? [y/N]` with No selected by default. Declining an explicit `--session` selection exits with status 1 as today; declining a navigation selection returns to navigation. Empty active lists still expose Show archived, which intentionally replaces the former forced-create flow in full-screen mode.

Confirmation dialogs bind `y` and `n` directly, and Enter accepts the indicated default, so `[Y/n]` and `[y/N]` remain truthful. Escape in an archived/stop/archive confirmation is No; Escape in the batch-resume dialog is Chat only. Ctrl+Q and empty-composer Ctrl+D share one Quit flow, including confirmation if any draft is unsent.

Agent rows show name, state, and server unread count. Selecting one exposes Attach, Resume, Stop, Retry delivery, and History settings with reasons when unavailable. New agent opens a form with configured provider choices, absolute working directory, optional name, and history policy. Default directory/history selection follows existing controller rules. History choices are literal and none; summary retains the existing refusal text.

Resume first uses the existing native conversation. A refusal offers the applicable explicit action: choose a new directory, change agent name, or confirm a fresh conversation. Fresh is never selected automatically. Stop and Archive confirmations name the affected agent/session; Archive explains that it stops session agents. Cancel performs no mutation. Repeated activation while work is pending cannot duplicate a request.

After a completed Archive, the server has already checkpointed and stopped that session's agents. Mark the controller closed, clear selection, and stop receiver/poller without issuing another checkpoint. Navigation becomes mandatory as at startup; Escape/Ctrl+C/Ctrl+Q exits, while the next committed selection restarts receiver/poller. Drafts remain in memory, but the 50-draft refusal never prevents leaving this post-archive state. If capacity is exhausted, the next session can still be read/operated; creation of another nonempty draft is refused until an existing draft is sent/cleared, with the same capacity notice. Structured TUI adapters route `/sessions` to navigation and `/archive` to `execute_action` plus this selection flow; they never invoke legacy `handle()`'s `close()`/`_pick_again` branches.

The palette includes New session, Switch session, Rename session, Archive session, New agent, Attach, Resume, Stop, Unread, Retry delivery, History settings, Clear draft, Refresh, Help, and Quit. In plain-channel mode it instead exposes Switch channel and Create channel alongside applicable chat commands. Actions and slash commands converge on the same controller operations.

The palette mirrors visible controls rather than being their only entry: session navigation has New session and More actions (Rename, Archive, Refresh); the agent area's More actions contains Resume, Stop, Unread, Retry delivery, and History settings; the composer exposes Clear draft with confirmation. Tab/Enter reaches each control and its menu without a mouse. Help and Quit also have the persistent footer shortcuts.

## 6. Activity, connection state, and errors

Show Connected, Connecting, or Reconnecting persistently. On disconnect, retain draft and history, identify status as stale, and prevent message submission. Keep API failures distinct from WebSocket connectivity; a live socket does not prove an HTTP action succeeded.

Routine agent state updates repaint rows rather than append repetitive chat messages. Show actionable notices near the relevant form or agent. Keep a bounded activity panel for detailed diagnostics and prior notices: 1,000 sanitized lines, with an omitted-count notice on overflow. Warnings remain reachable after a short toast disappears. Raw HTTP exception objects, authenticated URLs, and tokens never reach the UI.

Preserve user-facing recovery information: `catching up…`, `fresh`, `id unknown`, missing-directory guidance, wrapper-log path hints, and exact server refusal messages. Render public labels and diagnostic paths as plain sanitized text. Native IDs and authentication material are not exposed merely to fill an inspector.

For an API action, display progress, prevent duplicate invocation, and retain the relevant inputs on failure. A failed mutation is not retried automatically. Unexpected local errors must not be relabeled as connection failures; restore the terminal before surfacing them.

## 7. Existing behavior versus presentation changes

This document is an additive amendment to §5 of `2026-09-12-terminal-sessions-design.md` for the full-screen interactive view only. Until implemented and reviewed, the original CLI remains the reference behavior.

| Existing §5 behavior | Full-screen presentation |
|---|---|
| Numbered picker and `Choose:` | Searchable ID-keyed navigation with arrow selection and Enter |
| Empty list immediately prompts for creation | Empty state shows both New session and Show archived |
| One printed state line per agent | Persistent rows and inspector; same state/recovery labels |
| Sequential textual prompts | Forms/dialogs with equivalent values and confirmation defaults |
| `/sessions` checkpoints, then opens picker | Like F2, TUI navigation opens without checkpoint; checkpoint happens at switch commit or Quit. This avoids closing the old controller before a potentially cancelled selection. Plain mode retains its original order. |
| `/history` prints cached recent messages | Focus/jump to the conversation history without appending duplicate records |
| Attach pause buffer is flushed after detach | Renderer suspends; model keeps updating; restore paints current state once and retains bounded notices |
| Plain-channel chat prints lines | Full-screen channel view with existing channel semantics; `--plain` retains old rendering |

Literal confirmations and recovery strings remain visible in dialog bodies/details. The Windows message stays exactly `Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch.` Nested tmux guidance stays exactly `Switch back: tmux switch-client -l`. Shell output and legacy `--plain` output contracts are unchanged.

Literal placement contract (`<...>` denotes the same substituted value as the existing CLI; it is not displayed literally):

| Contract literal | TUI location |
|---|---|
| `Sessions`, `New session`, `Show archived` | Navigation title and visible buttons |
| `Choose:` and numbered rows | Explicitly amended to searchable navigation and Enter selection above |
| `Session name:` | New-session form field label |
| `fresh` | Agent row in place of starting after a fresh launch |
| `catching up…` | Agent row and history inspector during pending literal catch-up |
| `id unknown` | Agent row/inspector when native ID is absent |
| `⚠ cwd missing — /resume <agent> --cwd PATH` | Agent row/inspector, plus the optional directory-repair action |
| `Resume N stopped agents? [Y/n]` | Batch-resume dialog body; y/n and Enter/default bound |
| `archived` | Archived session row and unarchive dialog context |
| `Unarchive it? [y/N]` | Unarchive dialog body; No default |
| `Working directory:` | Spawn/resume form directory label |
| `History mode [none/literal]:` | History-choice form label; literal default |
| `Archive session? [y/N]` | Archive confirmation body alongside affected session name |
| `not running; resume with /resume <agent>` | Attach preflight failure notice |
| `failed to start; see <data_dir>/logs/wrapper-<agent_id>.log` | Agent row/inspector failure detail when data directory is known; retain the existing unknown-directory explanation otherwise |
| `Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch.` | Disabled-action explanation or attempted-action notice; chat continues |
| `Switch back: tmux switch-client -l` | Activity/notice sink after successful nested switch-client |
| `summary history mode is not available in this version; use literal or none` | Invalid-history action notice or form error |
| `Started server in tmux session yapp-server.` | Pre-screen stdout on auto-start, retained in activity |
| `Start it manually: python run.py` | Pre-screen stderr and exit 1 from applicable `ensure_server` failures on full-screen entry; companion `Server log: <data_dir>/logs/server.log`, and `Tmux session: yapp-server` only when existing confirmation rules permit it. Existing shell failure hint remains unchanged. |
| API refusal/checkpoint/startup failure text and log-path hints | Form/notice or pre-screen diagnostic, sanitized; no loss of recovery details |

Rows can reuse `_agent_line`/`_agent_status` wording, but the TUI must sanitize their returned fragments itself: `_agent_status` appends raw cwd/error/history-note values and currently relies on plain `show()` sanitization. A known failed-start log hint remains available in the expanded inspector even when its row is clipped. The original 09-12 spec's §5 and decision section cross-reference D16+ here; implementation activates only the full-screen presentation amendment after written-design approval.

## 8. Architecture and terminal ownership

Use prompt-toolkit `Application`, layout controls, buffers, and async event loop. The implementation raises the `requirements-cli.txt` constraint to `prompt-toolkit>=3.0.53,<4.0`, the locally verified baseline for `Application`, context-managed `create_pipe_input`, and `in_terminal`. Its full-screen layout and conditional-container APIs fit this design; its `in_terminal()` context manager detaches input, restores cooked mode, suspends painting, and repaints on return. No additional TUI framework is needed. Reference: [official full-screen guide](https://python-prompt-toolkit.readthedocs.io/en/master/pages/full_screen_apps.html) and the locally inspected `prompt_toolkit.application.in_terminal` implementation. No server/slice-1 implementation changes are needed; existing endpoints supply all view data.

Proposed boundaries:

- `cli.py`: entry routing and existing ChatClient transport/cache. Preserve helper re-exports and shell paths. Add optional structured view notifications for connection/message/cache changes so views never parse printed text.
- `cli_workspace_chat.py`: lifecycle authority, selection/version guards, checkpoint ordering, conditional polling, shared action dispatch. Extract presentation-dependent selection/dialog/attach hooks with the existing prompt adapter as defaults. Add structured action outcomes for the TUI; preserve legacy `handle()` return values via its adapter.
- `cli_tui.py`: application composition, startup/shutdown, focus routing, and terminal suspension integration.
- `cli_tui_view.py`: sanitized display fragments, responsive layout, conversation viewport, navigation rows, and in-memory presentation state.
- `cli_tui_dialogs.py`: palette, forms, confirmations, and contextual completion.

The TUI owns only presentation state: focus, search, viewport, drafts, notices, and modal state. `ChatClient` owns messages/connectivity and `WorkspaceChatController` owns selected workspace and lifecycle transitions. Widgets never open independent API clients, access stores/registries/provider files, or replay mutations. The sidebar list is a read-only projection fetched through a controller method using the same API facade.

The following are planned integration interfaces, not APIs already present at the baseline:

| Caller / interface | Responsibility |
|---|---|
| TUI → `controller.list_sessions(include_archived=False)` | Async read through the existing WorkspaceAPI; return the server list/warning without changing selection |
| TUI → `controller.select_session(ws_id)` | Transactional full-screen selection sequence defined in §5; also used by the select-session action |
| TUI → `controller.execute_action(action, payload)` | Async, validated shared operation; returns a structured completed/cancelled/failed outcome; failures cannot masquerade as successful command submission |
| TUI → structured controller command adapter | Preserve slash syntax and shared operations/outcomes. Intercept TUI `/sessions` and `/archive` as described in §5 rather than entering legacy picker/close branches; legacy `handle()` return strings stay unchanged. |
| TUI → `client.submit(text)` via a structured submission adapter | Preserve chat/channel command behavior while exposing whether transport accepted a message; no direct socket calls from widgets |
| TUI → `controller.close()` | Existing idempotent checkpoint-before-cancel on Quit |
| Application → `client.receive_forever()` / `controller.poll_forever()` | One existing receiver and one conditional session poller; application owns their task lifetime |
| Controller → injected presentation hooks | Await selection/forms/confirmations and terminal handoff; old prompt/print adapter remains default |
| `controller.on_view_change(event)` → TUI | Optional notification after selection, workspace revision, action progress/outcome, or notice; TUI reads authoritative controller state |
| `client.on_view_change(event)` → TUI | Optional notification after connectivity, messages/history/update/delete/clear, settings/channel, or status changes; TUI reads authoritative client state |

`execute_action` accepts an explicit allowlist: select/create/rename/archive session; spawn/resume/stop/attach agent; unread/retry/history policy. Session and agent selection use full stable IDs in payloads; user-written slash commands still use existing resolvers. The controller captures and validates the selected workspace before any mutation. Form values become typed/validated payload fields, not interpolated shell or slash strings. The legacy dispatcher delegates to these same operations. Event notifications are advisory invalidations and never replace workspace or message state with a second copy.

View notifications have one defined shape: `source` (`client` or `controller`), `kind` (`connection`, `messages`, `history`, `settings`, `status`, `channel`, `selection`, `agent_state`, `action`, or `notice`), `workspace_id` and `agent_id` (nullable strings), `message_ids` (tuple of existing message IDs, empty when inapplicable), `revision` (monotonic integer for that source), `selection_generation` (controller generation, null for unscoped client events), and `text` (optional plain notice text). Trailing optional `old_channel` and `new_channel` fields default to None; channel-renamed events carry exact old/new identities for draft migration even when settings arrive first. Existing positional fields remain unchanged. Controller revision means `_state_revision`; client revision is an invalidation counter, not a duplicate message store. A callback runs after the state mutation and applicable `_state_revision`/`_selection_version` bump, in the same event-loop tick. Never notify with pre-mutation state. Views use IDs/revisions to reject stale delayed presentation work and read the current authoritative model.

`execute_action` returns `ActionOutcome(status, message, workspace_id, agent_id)`: status is exactly `completed`, `cancelled`, or `failed`; message is optional plain diagnostic text; IDs are nullable stable strings identifying the attempted action. Expected refusals return failed; cancelled forms return cancelled and perform no action. Unexpected local exceptions propagate through terminal restoration. Structured legacy adapters expose the same outcome without changing their pre-existing boolean/string return contracts.

In TUI mode `ChatClient.output` is the bounded notice sink. With `on_view_change` set, `show_message()` and `history()` bypass printed transcript output and notify the view; the conversation reads `client.messages`. Controller `on_workspace` emits `agent_state` after mutation instead of calling `client.show()` for repetitive status lines. Legacy adapters retain those prints. Other `show()` calls go to the notice sink, never directly to terminal stdout. `pause_output()` and `resume_output()` are unused by the TUI: its renderer owns screen suspension, and the authoritative model remains live. Marshal worker-thread notice calls onto the loop before notifying/rendering.

All lifecycle mutations run through one serialized controller action path, with the selection ID captured before awaits; lock only executable action/commit work, never user-input dialogs. Preserve selection-generation and same-session revision guards; after initial selection, one conditional two-second poller and one receiver stay alive through forms, ordinary navigation, and terminal handoff. Completed Archive stops them until the next selection, as §5 specifies. UI notification hooks run on the event loop; worker-thread output is marshaled with thread-safe loop scheduling. Bound existing message storage at 10,000 records; rendering must not make an unbounded duplicate transcript.

Attach has two phases under an exclusive terminal-handoff guard. First perform platform/target/exact has-session preflight off-loop while the Application remains visible. Windows, missing-session `not running`, and other preflight `CLIError` results become notices without suspending. Extract a shared preflight helper from existing `attach_agent`; preserve `attach_agent(agent, *, runner=subprocess.run, output=print, shell_session=None) -> int` for shell callers. Inject `runner` into both preflight and foreground calls. Preflight performs no HTTP call, reads no token, discards probe output, and probes with exact `'=' + target` matching. Both phases map `OSError`/`SubprocessError` to the fixed `Could not attach to the agent terminal. Check tmux and retry.` notice; never expose tmux stderr. On nonzero foreground return, the TUI adapter reruns the exact has-session probe: if gone, show `not running; resume with /resume <agent>`; otherwise show a fixed attachment-failed status notice. Shell return-code and shell-form recovery-hint contracts remain unchanged.

After successful preflight, enter `in_terminal()` and run only the foreground attach/switch operation through a strongly referenced, shielded `asyncio.to_thread` task. Receiver and poller remain live. Any `attach_agent(output=...)` text and other `show()` calls go to the notice sink during handoff and appear after repaint. On detach or foreground failure, wait for thread completion, release terminal ownership, restore Application input/renderer, then render current state once. Keep focus, draft, and scroll-follow preference.

While foreground tmux owns the tty, Ctrl+C is handled by that terminal client/foreground session, not by the TUI's key binding; detach returns ownership. SIGINT/SIGTERM delivered to the CLI request graceful Quit, which waits on the handoff guard and shielded task. Do not repaint or exit while `to_thread` still owns the terminal; cancelling its awaiting coroutine alone cannot stop it. Do not kill provider terminals to recover UI ownership. After ownership returns, the normal checkpoint-before-task-cancel Quit path runs. An uncatchable process termination is outside graceful-shutdown guarantees.

Signal-driven Quit skips unsent-draft confirmation. Application startup registers SIGINT and SIGTERM with the event loop, falling back to `signal.signal` forwarding through `loop.call_soon_threadsafe` when loop registration is unavailable. Shutdown restores the exact prior process handlers and prior standard-asyncio loop callbacks, including their callback context. The Application runs with `run_async(handle_sigint=False)` so prompt-toolkit does not replace this ownership. The `Keys.SIGINT` binding remains for key-processor-delivered interrupts, distinct from the `c-c` text-editing binding; both registered signals enter the same quit coordinator. During pre-commit selection use the cancellation rule in §5; during commit or attach await its shielded operation. Key-driven Quit retains draft confirmation. Signal registration/restoration belongs to application startup/shutdown, not individual widgets.

Inside tmux, `switch-client` returns promptly, `in_terminal()` exits immediately, and the hidden app repaints. `Switch back: tmux switch-client -l` is retained as a notice; returning to the TUI's client shows current state. Outside tmux, foreground attach owns the tty until detach. Nonzero foreground results become recoverable notices after restoration.

## 9. Validation and acceptance

Use real application/controller paths with prompt-toolkit pipe input and controlled output. Assert visible state, focus, key behavior, and API effects rather than only matching widget constructors. Keep existing shell and legacy-prompt tests. `DummyOutput.get_size()` is fixed at 40×80; resize tests must use a subclass overriding `get_size()` with a mutable `Size`, or `Vt100_Output(StringIO(), get_size=..., enable_cpr=False)`. Capture `renderer.last_rendered_screen.data_buffer` after render callbacks for row/cell assertions; a fragment list alone does not prove layout.

Required coverage:

1. Navigation search, duplicate names, reorder-preserved selection, archived-only empty state, explicit archived decline, and initial Escape/Ctrl+C exits with no checkpoint/receiver.
2. Create/select/rename/archive workflows; form cancellation and failure retain input and cannot double-submit; pre-commit cancellation preserves `_closed`/channel/draft, Quit waits for shielded commit, same-selection no-op preserves state, and completed Archive returns to mandatory navigation without double checkpoint (R-C/R-D).
3. Compose, multiline paste, Enter/completion behavior, transport failure retention, per-session drafts, exact 50-draft notice including post-archive capacity behavior, Ctrl+C draft preservation, confirmation y/n/defaults, scroll anchoring/new-count behavior, updates/deletes/reconnect deduplication.
4. Agent forms/actions, missing-directory and fresh-resume recovery, summary refusal, literal status/defaults, and Windows chat with tmux actions refused.
5. Actual receiver/poller updating during open dialogs and controlled blocked attach; preflight refusal never suspends; suspension precedes foreground attach; signal during handoff waits; return/cancellation/error restores ownership and repaints once; disappearing-target re-probe uses exact matching.
6. Headless resize cases at 120×30, 80×24, and 70×16; restore focus/draft after resize. Plain-text sanitization for malicious escape/control content in every server-derived field.
7. CLI dispatch for default/full-screen/`--plain`/`--channel`, exact single fallback stderr line/unchanged exit, non-terminal stdin refusal, POSIX-only TERM rule, Windows unset TERM remaining full-screen, and unchanged shell command output/JSON (R-E).
8. Isolated real-server smoke: create session, send/read, switch sessions, quit/checkpoint. Use temporary ports/data/uploads and inert provider/tmux fixtures only, with registered cleanup and redacted logs.

Manual terminal QA must exercise Linux tmux outside and inside another tmux client, keyboard-only operation, mouse operation, selection/copy, wide/compact resize, multiline paste, and error dialogs. Record what was actually exercised. Headless tests do not establish visual quality or real provider behavior; provider processes in automated tests remain inert shims.

Windows full-screen operation remains an explicitly not-exercised-end-to-end limit. Unit tests cover dispatch and tmux refusals; `--plain` remains the documented recovery path.

Concrete headless scenario (test-harness sketch; helper names are to be defined in the implementation plan):

```python
async def test_session_selection_message_and_quit():
    # In-memory API fixture: billing and frontend, no agents or paid launches.
    # Pipe-input drives the real Application/controller; sized output supplies I/O.
    # A render observer captures last_rendered_screen.data_buffer and focus IDs.
    async with tui_harness(sessions=[billing, frontend]) as ui:
        await ui.key("F2")
        await ui.type_text("billing")
        await ui.key("ENTER")
        await ui.wait_for_selected_session(billing["id"])
        assert ui.focused_control == "composer"
        await ui.type_text("Please review retries")
        await ui.inject_workspace_state("claude-1", "running")
        # That helper calls client.handle_event({'type': 'workspace', 'data': ...})
        # with a complete server-shaped snapshot; it never mutates TUI internals.
        assert ui.composer_text == "Please review retries"
        await ui.key("ENTER")
        await ui.wait_for_server_echo("Please review retries")
        assert ui.message_occurrences("Please review retries") == 1
        await ui.key("CTRL_Q")
        await ui.wait_closed()
        assert ui.api.checkpoints == [billing["id"]]
        assert ui.cleanup_events.index("checkpoint") < ui.cleanup_events.index("receiver_cancel")
```

The harness uses bounded event-based waits, never arbitrary sleeps to guess readiness. A separate PTY smoke captures real terminal output for visual review; DummyOutput alone cannot prove clipping, colors, or cursor restoration.

Acceptance: a new user can create a session, start an agent, attach, return, and send a message using visible actions and keyboard navigation; normal state updates do not flood the conversation; drafts and cursor position survive incoming messages, dialogs, and session changes.

## 10. Decisions and review boundary

| Decision | Choice and cost |
|---|---|
| D16 | User selected full-screen over line-oriented polish. Reuse shell/server behavior; change the interactive view and controller presentation hooks. Cost: a new rendering surface to validate. |
| D17 | Reuse prompt-toolkit rather than add another runtime. Cost: compose controls instead of adopting a new widget framework. |
| D18 | Keep a `--plain` escape hatch. Cost: two presentation adapters need regression coverage; core operations stay shared. |
| D19 | Compact layouts use overlays, not squeezed permanent panes. Cost: one additional action to inspect agents/sessions on smaller terminals. |
| D20 | Amend the empty-session picker and full-screen history/attach presentation as listed above; lifecycle/security constraints stand. Cost: full-screen help/tests differ from legacy text presentation. |
| D21 | Preserve reviewed `feature/terminal-sessions` at `ec8067c`; work on `feature/terminal-tui`. No merge or push implied. Cost: later integration must account for the stacked branch. |
| D22 (R-A) | TUI `/sessions` opens navigation without checkpoint; switch commit/Quit checkpoint instead. Plain order is unchanged. Avoids un-closing an abandoned controller selection; no observable checkpoint loss. |
| D23 (R-B) | Non-terminal stdout, or POSIX unset/empty/dumb TERM, auto-falls back to plain mode with one stderr line and no fallback-specific exit change; non-terminal stdin remains rejected. Cost: piped output uses plain rendering. Windows distinction is clarified by D28. |
| D24 | Require prompt-toolkit >=3.0.53,<4.0 for verified context-manager/test APIs. Cost: older installations must upgrade this existing dependency. |
| D25 | At the 50-unsent-draft limit, refuse a new destination with a notice instead of adding eviction UI. Cost: send/clear a draft before opening another destination. |
| D26 (R-C) | Completed Archive clears selection and closes controller; mandatory navigation and stopped receiver/poller last until next selection. No extra checkpoint or 50-draft navigation refusal. Cost: pick or quit after archiving; if draft capacity is full, further composing waits for a free slot. |
| D27 (R-D) | Quit cancels pre-commit selection preparation, but awaits shielded commit before closing. No lock across input dialogs. Cost: Quit may wait one bounded checkpoint. Signal Quit skips draft confirmation. |
| D28 (R-E) | TERM fallback is POSIX-only; Windows unset TERM still allows full-screen chat with tmux refusals. Cost: Windows users may need --plain for an untested full-screen issue. |

Next gate: Claude's implementation-plan review, then execution with reviewed tasks. Written-design approval is recorded; no further design or execution-choice permission is needed. No TUI implementation is included in this design change.
