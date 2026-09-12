# Full-screen terminal UX

Date: 2026-09-13
Status: Draft for user review. Full-screen direction approved; implementation has not started.
Branch: `feature/terminal-tui`, based on reviewed CLI head `ec8067c`.

## 1. Outcome and scope

Make everyday session work discoverable without remembering slash commands. The user can find a session, read its conversation, see agent state, write a message, and launch or attach to an agent from one persistent terminal screen.

The first version includes searchable session navigation, a scrollable conversation, a multiline composer, agent status and actions, guided forms, a command palette, and keyboard help. It reuses the authenticated HTTP/WebSocket interfaces and existing lifecycle controller. Shell commands retain their output, arguments, and scripting behavior.

This is the TUI slice. It does not implement the separately deferred summary-history slice, provider-terminal emulation, file editing, attachment uploading, or release packaging. Existing server commands remain accessible through the composer.

## 2. Entry points and compatibility

- `python cli.py` and `python cli.py chat` open the full-screen UI by default.
- `--session NAME` opens that session and keeps the existing stopped-agent resume decision. `--no-resume` suppresses that decision.
- `--channel NAME` opens full-screen plain-channel chat. The sidebar lists channels; session-only actions are absent. Existing channel commands continue to work.
- A new interactive-only `--plain` flag runs the existing line-oriented interface. It works on the bare invocation and the explicit `chat` command. Passing it with a shell command is rejected before configuration or network work.
- Full-screen mode requires terminal stdin and stdout. Redirected output and `TERM=dumb` produce a short explanation with the `--plain` alternative; non-terminal input still directs scripts to `read` or `send`. Screen-reader users can choose `--plain` explicitly.
- Existing auto-start rules remain: interactive local chat only, never auto-start with explicit `--url`, no server restart or duplicate-session replacement. Startup diagnostics appear before screen entry and remain available in the UI's activity panel.
- Exiting checkpoints the selected session and leaves server and agents running.

## 3. Layout and visual language

Use the terminal's normal background and foreground, one cyan accent for selection/focus, muted separators, and semantic success/warning/error accents. State always has a readable label; color and symbols are supplementary. Never render server-supplied text as ANSI or markup. Sanitize each display fragment before layout: single-line labels cannot contain tabs/newlines or terminal controls; message bodies may retain newlines and expand tabs to spaces. Style names come only from application code.

Wide layout, at least 110 columns and 24 rows:

```text
 agentchattr    billing                         Connected
┌ Sessions ───────────┬ Conversation ──────────────────────┐
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
 F2 Sessions   F3 Agents   F4 Commands   F1 Help   Ctrl+Q Quit
```

The left sidebar is 26 columns wide. The main pane holds the conversation, a compact agent area, and a composer that grows from three to six lines. An expanded agent inspector shows provider, directory, unread count, history state, resume availability, and relevant recovery guidance. Long labels truncate by display width; focusing a row exposes its full label in the inspector/help area.

At 80–109 columns or 18–23 rows, navigation becomes an overlay opened with F2, and the agent area becomes a compact status row opened with F3. Conversation and composer retain the usable space. Below 80 columns or 18 rows, show a compact resize message plus Help and Quit; keep connection state, draft, and background work intact. Resizing back restores the focused control without losing input.

Empty states are actionable: “No sessions yet” with New session and Show archived; “No agents in this session” with New agent; “No messages yet” with composer guidance. An empty active-session list must not hide archived sessions.

## 4. Navigation and message composition

| Input | Behavior |
|---|---|
| F1 | Help overlay with currently available actions and shortcuts |
| F2 | Focus/open session or channel navigation and search |
| F3 | Focus/open agents and the selected agent's actions |
| F4 | Open searchable command palette |
| Tab / Shift+Tab | Move focus between controls; accept/cycle an open completion menu first |
| Up / Down | Move the focused list or completion selection; normal cursor movement in the composer |
| Enter | Activate a selected action; in composer, send or execute the current input |
| Alt+Enter, also Esc then Enter | Insert a composer newline across terminals without distinct Shift+Enter support |
| PageUp / PageDown | Scroll conversation when its pane has focus |
| End | Jump to the latest message when conversation has focus |
| Escape | Dismiss the current overlay or completion, then restore prior focus |
| Ctrl+C | Cancel the current form/overlay, or clear the composer; never stop agents |
| Ctrl+Q | Quit, confirming first if any unsent draft exists |
| Ctrl+D | In an empty composer, request Quit; otherwise normal forward-delete behavior |

Mouse selection, buttons, and wheel scrolling are supported, but every operation is keyboard-accessible. Buttons have full text labels. Single-letter actions never capture typing in text fields.

The composer supports multiline paste and suggestions for commands, agent mentions, and applicable arguments. Pasting never submits automatically. Enter accepts a highlighted completion before sending. A visible hint explains Enter and Alt+Enter when the composer has focus.

Preserve one in-memory draft per visited session/channel, bounded to 50 drafts of at most 64 KiB of UTF-8 text each. Empty drafts do not consume a slot. If all 50 slots contain unsent text, ask the user to discard a chosen draft or cancel switching; never silently evict unsent text. Drafts are not written to provider files or persisted to disk. Cap composer input at 64 KiB with an explanatory message.

Do not clear the composer when disconnected, when a command fails, or when a form is cancelled. Clear a message after the existing transport accepts the send; display the server echo as the conversation record. Do not describe socket acceptance as server acknowledgment. Transport failure/uncertainty retains the draft and explains that retry is manual. There is no automatic message replay.

When the user is at the bottom, follow new messages. When they scroll away, preserve the viewport and show a local “N new” affordance that jumps to the bottom. This counter is distinct from the server's per-agent unread count. Updates, deletions, and reconnect history reconcile by message ID and do not duplicate displayed messages.

## 5. Session and agent workflows

Session search is a case-insensitive local filter over fetched names and IDs. Rows are keyed by full ID, not display names. Preserve selected ID across refresh/reordering. Use existing newest-updated ordering and duplicate-name ID suffixes. Show archived state explicitly. Refresh on entry, explicit Refresh, completed session changes, and reconnect; do not introduce unconditional background list polling.

Selecting a session runs the controller's existing lifecycle sequence: checkpoint the old selection, retrieve the new workspace, resolve archived/resume prompts, select its channel, and request reconciliation. Keep the old selection if listing/loading fails before the switch commits. Disable sending and competing lifecycle actions during a switch. Draft and selection are never changed by a stale response. Opening navigation alone does not checkpoint or clear selection.

For a stopped-agent batch, display the exact question `Resume N stopped agents? [Y/n]` with Resume and Chat only choices. Initial default is Resume; Escape cancels the choice and keeps the prior selection. `--no-resume` means Chat only without prompting. Missing-directory agents are explained and excluded as today. On Windows, show the existing tmux limitation and enter chat without launching.

Selecting an archived session opens `Unarchive it? [y/N]` with No selected by default. Declining an explicit `--session` selection exits with status 1 as today; declining a navigation selection returns to navigation. Empty active lists still expose Show archived, which intentionally replaces the former forced-create flow in full-screen mode.

Agent rows show name, state, and server unread count. Selecting one exposes Attach, Resume, Stop, Retry delivery, and History settings with reasons when unavailable. New agent opens a form with configured provider choices, absolute working directory, optional name, and history policy. Default directory/history selection follows existing controller rules. History choices are literal and none; summary retains the existing refusal text.

Resume first uses the existing native conversation. A refusal offers the applicable explicit action: choose a new directory, change agent name, or confirm a fresh conversation. Fresh is never selected automatically. Stop and Archive confirmations name the affected agent/session; Archive explains that it stops session agents. Cancel performs no mutation. Repeated activation while work is pending cannot duplicate a request.

The palette includes New session, Switch session, Rename session, Archive session, New agent, Attach, Resume, Stop, Unread, Retry delivery, History settings, Refresh, Help, and Quit. In plain-channel mode it instead exposes Switch channel and Create channel alongside applicable chat commands. Actions and slash commands converge on the same controller operations.

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
| `/sessions` immediately opens picker | Controller checkpoints, then opens navigation; ordinary F2 opening does not checkpoint |
| `/history` prints cached recent messages | Focus/jump to the conversation history without appending duplicate records |
| Attach pause buffer is flushed after detach | Renderer suspends; model keeps updating; restore paints current state once and retains bounded notices |
| Plain-channel chat prints lines | Full-screen channel view with existing channel semantics; `--plain` retains old rendering |

Literal confirmations and recovery strings remain visible in dialog bodies/details. The Windows message stays exactly `Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch.` Nested tmux guidance stays exactly `Switch back: tmux switch-client -l`. Shell output and legacy `--plain` output contracts are unchanged.

## 8. Architecture and terminal ownership

Use the already-installed prompt-toolkit 3.x `Application`, layout controls, buffers, and async event loop. Installed baseline is 3.0.53. Its full-screen layout and conditional-container APIs fit this design; its `in_terminal()` context manager detaches input, restores cooked mode, suspends painting, and repaints on return. No additional TUI framework is needed. Reference: [official full-screen guide](https://python-prompt-toolkit.readthedocs.io/en/master/pages/full_screen_apps.html) and the locally inspected `prompt_toolkit.application.in_terminal` implementation.

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
| TUI → `controller.execute_action(action, payload)` | Async, validated shared operation; returns a structured completed/cancelled/failed outcome; failures cannot masquerade as successful command submission |
| TUI → `controller.handle(text)` via a structured command adapter | Preserve slash syntax and legacy delegation; the adapter also exposes the structured outcome without changing existing legacy return strings |
| TUI → `client.submit(text)` via a structured submission adapter | Preserve chat/channel command behavior while exposing whether transport accepted a message; no direct socket calls from widgets |
| TUI → `controller.close()` | Existing idempotent checkpoint-before-cancel on Quit |
| Application → `client.receive_forever()` / `controller.poll_forever()` | One existing receiver and one conditional session poller; application owns their task lifetime |
| Controller → injected presentation hooks | Await selection/forms/confirmations and terminal handoff; old prompt/print adapter remains default |
| `controller.on_view_change(event)` → TUI | Optional notification after selection, workspace revision, action progress/outcome, or notice; TUI reads authoritative controller state |
| `client.on_view_change(event)` → TUI | Optional notification after connectivity, messages/history/update/delete/clear, settings/channel, or status changes; TUI reads authoritative client state |

`execute_action` accepts an explicit allowlist: select/create/rename/archive session; spawn/resume/stop/attach agent; unread/retry/history policy. Session and agent selection use full stable IDs in payloads; user-written slash commands still use existing resolvers. The controller captures and validates the selected workspace before any mutation. Form values become typed/validated payload fields, not interpolated shell or slash strings. The legacy dispatcher delegates to these same operations. Event notifications are advisory invalidations and never replace workspace or message state with a second copy.

All lifecycle mutations run through one serialized controller action path, with the selection ID captured before awaits. Preserve selection-generation and same-session revision guards; one conditional two-second poller and one receiver stay alive through forms, navigation, and terminal handoff. UI notification hooks run on the event loop; worker-thread output is marshaled with thread-safe loop scheduling. Bound existing message storage at 10,000 records; rendering must not make an unbounded duplicate transcript.

For Attach, acquire an exclusive terminal-handoff guard, suspend the Application with `in_terminal()`, and run the existing attach helper in `asyncio.to_thread`. Do not cancel receiver/poller. On normal detach or attach failure, restore and invalidate once using current model state. Keep focus, draft, and scroll-follow preference. While the foreground process owns the terminal, a cancellation request must wait for terminal ownership to return before repainting or exiting; cancelling an asyncio task does not stop its worker thread. Do not kill provider terminals to recover UI ownership.

Inside tmux, `switch-client` returns promptly; the hidden application's model may continue updating. Returning to its tmux client shows current state and the switch-back guidance. Outside tmux, foreground attach owns the tty until detach. Nonzero attach results and missing sessions become visible recoverable notices.

## 9. Validation and acceptance

Use real application/controller paths with prompt-toolkit pipe input and controlled output. Assert visible state, focus, key behavior, and API effects rather than only matching widget constructors. Keep existing shell and legacy-prompt tests.

Required coverage:

1. Navigation search, duplicate names, reorder-preserved selection, archived-only empty state, and explicit archived decline.
2. Create/select/rename/archive workflows; form cancellation and failure retain input and cannot double-submit; serialized switching preserves checkpoint order and stale-response guards.
3. Compose, multiline paste, Enter/completion behavior, transport failure retention, per-session drafts and capacity handling, scroll anchoring/new-count behavior, updates/deletes/reconnect deduplication.
4. Agent forms/actions, missing-directory and fresh-resume recovery, summary refusal, literal status/defaults, and Windows chat with tmux actions refused.
5. Actual receiver/poller updating during open dialogs and controlled blocked attach; suspension precedes attach; return/cancellation/error restores terminal ownership and repaints once without duplicate output.
6. Headless resize cases at 120×30, 80×24, and 70×16; restore focus/draft after resize. Plain-text sanitization for malicious escape/control content in every server-derived field.
7. CLI dispatch for default/full-screen/`--plain`/`--channel`, non-terminal and dumb-terminal diagnostics, and unchanged shell command output/JSON.
8. Isolated real-server smoke: create session, send/read, switch sessions, quit/checkpoint. Use temporary ports/data/uploads and inert provider/tmux fixtures only, with registered cleanup and redacted logs.

Manual terminal QA must exercise Linux tmux outside and inside another tmux client, keyboard-only operation, mouse operation, selection/copy, wide/compact resize, multiline paste, and error dialogs. Record what was actually exercised. Headless tests do not establish visual quality or real provider behavior; provider processes in automated tests remain inert shims.

Concrete headless scenario (test-harness sketch; helper names are to be defined in the implementation plan):

```python
async def test_session_selection_message_and_quit():
    # In-memory API fixture: billing and frontend, no agents or paid launches.
    # Pipe-input drives the real Application/controller; DummyOutput supplies I/O.
    # A render observer captures sanitized fragments plus focused control IDs.
    async with tui_harness(sessions=[billing, frontend]) as ui:
        await ui.key("F2")
        await ui.type_text("billing")
        await ui.key("ENTER")
        await ui.wait_for_selected_session(billing["id"])
        assert ui.focused_control == "composer"
        await ui.type_text("Please review retries")
        await ui.inject_workspace_state("claude-1", "running")
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

Next gate: user review of this concrete design, plus Claude's technical feedback. After approval, write the implementation plan and execute it with reviewed tasks. No TUI implementation is included in this design change.
