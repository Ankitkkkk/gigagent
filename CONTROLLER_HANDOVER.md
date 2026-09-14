# Controller Handover — Claude ↔ Codex coordination on `feature/terminal-sessions`

Written 2026-09-13 ~02:00 IST, last updated ~02:15 at session shutdown, by Claude (controller/reviewer). Read this first if
you are the next Claude session taking over review duty. Codex keeps its own
rolling checkpoints at the top of `SPAWN_SESSION_HANDOVER.md`; read those
second. Both files are untracked on purpose (shared scratch, not product).

## 1. Who does what

| Role | Who | Owns |
|---|---|---|
| Implementer | Codex (external agent; spawns its own workers such as `/root/cli_task4`) | Source + tests, one task at a time, commits |
| Controller / reviewer / ledger | Claude (this role) | Task reviews via subagents, rulings, ledgers, spec edits, final whole-branch review |
| Authoriser | the user | Which slice to build, integration decision (merge / PR / keep) |

Nobody merges or pushes. Nobody runs `git commit -a`. Nobody launches a real
`claude`/`codex` from tests (a PATH-shim `kilo` stub stands in for providers).

## 2. The message channel — `AGENT_MESSAGES.md` (repo root)

**Format.** Append-only. One entry per message:
```
### [YYYY-MM-DD HH:MM] <from> → <to> — <subject>
body
```
Codex's entries sometimes carry the label `codex continuation` or just `codex`
instead of a clock time, and its clock ran ~25 min behind mine at one point.
**Append order is authoritative, not timestamps.** Never edit or delete
another agent's entry. Tags in subjects: `QUESTION`, `RULING`, `BLOCKING`,
`ACK`.

**Gates** (the only handshakes that move work):
- Codex posts `Task N done: <sha>` → Claude packages `<prev-head>..<sha>` and
  dispatches a task review → Claude posts either `Task N review clean` or
  `Task N review: <k> Important, fix round R` with numbered findings.
- Codex posts `Task N fix done: <sha>` → Claude runs a *scoped* re-review on
  the fix range only → `Task N review clean` (or another round; max 5).
- Plans: `Slice K plan ready: <path>` → plan review → `Plan approved` or an
  ordered revision list → `Slice K plan revised: <sha>` → delta re-review.
- Final: `Fix wave done: <a>..<b>` after the whole-branch review's single fix
  wave → one scoped re-review → residuals adjudicated → user gets the menu.

**Overlap rule** (standing): Codex may implement task N+1 while task N is under
review *only* if N+1 touches no file N changed; it commits N+1 only after
`Task N review clean`. If files overlap, it waits. This has been honoured
every time; Codex announces "disjoint … started" and "commit gate released".

**Questions and rulings.** Codex asks with `QUESTION`; Claude answers with a
`RULING` entry that states the decision, the reason, and the cost if wrong,
and mirrors it into the ledger as `Ruling: … — cost if wrong: …`. Codex has
also proposed rulings itself (they were all sound); accept them explicitly
with `ACK` and ledger them "raised by Codex, accepted". When Codex spotted a
defect in my own plan text (it found ~15 across both slices), that is a
plan-mandated finding: rule, don't defend the plan.

**Coverage during outages.** When my reviewer subagent died on a rate limit
(HTTP 429, 2026-09-12 ~16:10–16:30), Codex covered controller duties for
~20 min: ran a local review, gated commits, made one ruling, wrote amendment
files. On return I confirmed its rulings, accepted its local review as the
round-0 task review (ruling, ledgered), and resumed. Pattern to repeat: post
"back online; resuming review duty", confirm or overturn each interim ruling
explicitly, then continue.

**Watcher.** I do not poll the file. A persistent Monitor emits an event on
every new non-Claude `### [` header and every new commit:
```
cd /home/fa064152/projects/personal/agent-collab/agentchattr
last_lines=$(wc -l < AGENT_MESSAGES.md); last_head=$(git rev-parse HEAD)
while true; do sleep 5
  cur=$(wc -l < AGENT_MESSAGES.md 2>/dev/null || echo 0)
  [ "$cur" -lt "$last_lines" ] && last_lines=0
  if [ "$cur" -gt "$last_lines" ]; then
    sed -n "$((last_lines+1)),${cur}p" AGENT_MESSAGES.md | grep -E --line-buffered '^### \[' | grep -v --line-buffered 'claude →' || true
    last_lines=$cur; fi
  h=$(git rev-parse HEAD 2>/dev/null || echo "$last_head")
  [ "$h" != "$last_head" ] && { echo "NEW COMMIT: $(git log --oneline -1)"; last_head=$h; }
done
```
Re-arm it (Monitor tool, `persistent: true`) in a new session. On each event,
read the entry with `awk '/<subject>/{p=1} p' AGENT_MESSAGES.md`.

## 3. How reviews are run (mechanics)

- SDD skill scripts: `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.3.0/skills/subagent-driven-development/scripts/`
  - `task-brief PLAN N` → `<workspace>/task-N-brief.md` (already extracted for
    every slice-2 task).
  - `review-package PLAN BASE HEAD` → `<workspace>/review-BASE..HEAD.diff`
    (commit list + stat + full diff). Reviewers read this file, never git.
- Workspaces (git-ignored, keep them): `.superpowers/sdd/2026-09-12-terminal-sessions-server-core/`
  (slice 1) and `.superpowers/sdd/2026-09-12-terminal-sessions-cli/` (slice 2).
  Each has `progress.md` — the ledger. First line names its plan. Codex also
  drops `task-N-report.md`, amendment files, and its own notes there.
- Dispatch shape for a task review: brief path + global constraints copied
  verbatim + report path + BASE/HEAD/diff path + 2–4 *named risks* (specific
  cross-module checks the diff alone can't show) + the output format from
  `task-reviewer-prompt.md`. Re-reviews use `re-review-prompt.md` with the
  findings verbatim and FIX_BASE = the head the previous review saw.
- Model choices used: `haiku` for transcription tasks / one-function fixes;
  `sonnet` for most task reviews and scoped re-reviews; `opus` for the plan
  reviews, the two largest tasks (launcher, routes; CLI startup/picker and
  commands), and the whole-branch review.
- Reviewers read files **on disk**, so Codex's unstaged edits are visible to
  them; when a review of commit X is running while Codex edits the same file,
  tell the reviewer to reason from the diff and not run that test file.
- Never run the full suite while Codex is mid-edit; run it on a clean tree
  before the finishing step. `python` = `/tmp/agentchattr-cli-venv/bin/python`
  (plain `python` is unavailable on this box).
- `⚠ Cannot verify from diff` items in a review are the controller's to
  resolve before closing the task.

## 4. State right now — SUPERSEDED, see §8 (slice 2 complete at ec8067c)

> **Two-controller incident (2026-09-13).** While this session (A) was
> shutting down, a second Claude session (B) resumed from this handover and
> became the active controller; both wrote to the same ledger and channel for
> ~30 minutes, and A's "NOT reviewed / retro-confirm" ledger lines were wrong
> (B had already reviewed Tasks 4 and 5). Rule: **one controller session at a
> time**; on resume, first grep `AGENT_MESSAGES.md` for `claude →` entries
> newer than the handover and read the ledger's last 20 lines before
> dispatching anything. The table below is A's last view; B's ledger entries
> supersede it.


**Slice 1 (server core): complete, unmerged.** HEAD `eaa7a2b` at completion;
343 tests OK (2 skips); whole-branch review + fix wave done; 30 rulings and
29 deferred minors in its ledger. Integration decision deferred — "keep the
branch as-is" is in effect until slice 2 lands, unless the user says merge
first.

**Slice 2 (CLI): in progress.** Plan `docs/superpowers/plans/2026-09-12-terminal-sessions-cli.md`
(`cc0460f`, approved after one revision round). Spec §5 amended twice
(`c7adbe9`, `cc4cb25`).

| Task | Commit(s) | State |
|---|---|---|
| 1 HTTP + resolution | `fdaf9ef` + fix `48f4c72` | clean |
| 2 shell commands + parser | `259594d` + fix `5dbe7b8` | clean |
| 3 auto-start + picker | `cef9a50` + fix `62b8eaa` | clean |
| 4 chat commands, events, checkpoint lifecycle | `aa3ec20` | **review NOT completed** — the reviewer subagent was killed when the Claude session shut down (2026-09-13 ~02:15). **First action on resume: re-dispatch the Task 4 review** with brief `task-4-brief.md`, report `task-4-report.md`, package `review-62b8eaa..aa3ec20.diff` (already built), named risks: checkpoint calls via `to_thread` before the socket closes + warning on 409/503; `settings` re-assert runs after `ChatClient.handle_event`'s rewrite and on reconnect; conditional poll stops when nothing is starting/pending and cannot double-run after `/sessions`. |
| 5 foreground attach | — | Codex's disjoint half (`cli_workspaces.py` helpers + `tests/test_cli_workspace_commands.py`) is GREEN (44 tests) and uncommitted; the `cli.py`/`cli_workspace_chat.py` half waits for `Task 4 review clean`. Accepted interface: `ChatClient.show(text, *, immediate=False)`; picker renders immediately while chat output is buffered. |
| 6 end-to-end + docs | — | not started |

After Task 6 clears: build `review-package PLAN eaa7a2b HEAD`, dispatch the
whole-branch review (opus) with the slice-2 ledger's rulings/deferred minors
attached, hand Codex one fix wave, one scoped re-review, adjudicate residuals,
run the full suite + `git diff --check` on the final HEAD, then present the
finishing menu (merge locally / push+PR / keep) for the **whole branch**.

## 5. Rulings made in slice 2 (all also in the CLI ledger)

1. CLI offers `none`/`literal` only, `literal` default; explicit `summary`
   refused with the server's message; §5's size-based default suspended
   until slice 3.
2. No flag overloading: `--history-mode`, `--agent-name`; `--history`/`--name`
   keep their existing meanings; `--limit` numeric.
3. Auto-start only for interactive chat and never with `--url`; shell
   commands print the manual hint only (no log path / tmux name — that
   detail is the interactive contract).
4. Module split `cli_api.py` / `cli_workspaces.py` / `cli_workspace_chat.py`;
   `ChatClient` and `cli.py` entry point unchanged.
5. `WorkspaceCommandResult(data, workspace)`: `--json` = raw server data.
6. Plan-review rulings: delete the phantom shell `checkpoint` command; keep
   the `⚠` sentinel; spec §5 changes for `catching up…`, `/attach` inside
   tmux via `switch-client`, `/history` no-arg unchanged, picker `fresh`
   marker + 4-hex id suffix + archived `--session` prompt + `sessions
   --archived`, auto-start passes explicit `run.py` flags and refuses when
   `agentchattr-server` already exists, polling only while
   starting/pending/WS-down.
7. `/attach` (handler, help, completion) deferred wholly to Task 5, no
   placeholder (Codex proposal, accepted).
8. Task 2 observation (2) rejected: shell hints don't carry log/tmux details.
9. Task 3 fold-ins: tmux-session hint only if `has-session` confirms;
   `--fresh` hint only when the 409 text contains `--fresh`; batch resume
   sends `{}` (never re-points cwd).

Deferred minors (slice 2, all CAN WAIT unless the final review says
otherwise): duplicate `_safe`/`terminal_text` filters; duplicated fallback
string in `_error_message`; stale `--json` availability text; `--session`
on subparsers that ignore it; unanchored `Archived …` strings; redundant
`websockets` import in `shell_command`; `(archived)` decoration unasserted;
ambiguous `·` join; fragment-only picker assertions; message-equality down
detection; readiness poll aborts on `CLIError` + tmux stderr discarded;
`chat --url` now needs `config.toml`; redundant `require_tmux_platform` after
the win32 early return; duplicated `has-session` argv.

## 6. Gotchas learned the hard way

- `cat >> FILE` with a typo'd filename and no heredoc hangs on stdin forever;
  it took a `TaskStop` to kill. Always use a quoted heredoc.
- `git commit -am` once swept the user's unrelated edits into a spec commit
  (`c9fda51`). Stage explicit paths only.
- Brief prose test counts drift; the brief's code block is authoritative.
- A tmux session created on an *existing* tmux server inherits that server's
  env, not the caller's — hence auto-start passes `run.py` flags explicitly.
- prompt-toolkit `prompt_async(default=…)` prefills the buffer; "n" + typed
  "y" became "ny". Defaults apply only on blank input now.
- `run_in_terminal` is a no-op after `prompt_async` returns; `/attach` runs
  the blocking `tmux attach` via `asyncio.to_thread` instead.
- Inside tmux, `tmux attach` is refused; use `switch-client`.
- The middleware allows agent bearer tokens on `/api/messages`; the slice-1
  fix filters that path through the visibility predicate rather than 403.
- Codex's worker paths (`/root/cli_taskN`) are Codex's own sandbox names,
  not paths on this machine.

## 7. Resume checklist for a new Claude session

1. `git log --oneline -15` and `git status --short` — expect only the two
   untracked docs (plus whatever Codex is mid-edit on).
2. Read `AGENT_MESSAGES.md` from the last `claude →` entry onward; act on any
   `Task N done`, `QUESTION`, or `fix done` that has no Claude reply.
3. Re-arm the watcher (section 2).
4. Read `.superpowers/sdd/2026-09-12-terminal-sessions-cli/progress.md`; the
   last lines say which review is dispatched or pending.
5. Continue the gate loop; keep the ledger; never merge/push.

## 8. Status update — 2026-09-13 00:12 IST (session B, post-restart)

**Both slices complete and review-clean. HEAD `ec8067c`. Nothing merged or pushed. Awaiting the user's integration decision (merge locally / push + PR / keep).**

What happened after the §4 snapshot:
- A stale pre-restart Claude session ("A") briefly ran alongside this one and wrote two inaccurate "outage coverage" lines in the CLI ledger; they are marked superseded there and session A stood down. Codex's local Task 4 review was accepted as *supplementary* (union ruling), not as the gate.
- Task 4: opus review + Codex local review → fix round 1 (I1 state-revision guard, narrowed except, two folded minors) → `270936f`, re-review clean.
- Task 5: `36fd2cd`, opus review Approved, 0 Important. Fold-in ruling: picker selection requests one reconcile GET.
- Task 6: `4bcca63` → fix round 1 (`--no-resume` on the top-level parser, ruled production edit; README parse guard; bounded MCP retry) → `6ed9886`, re-review clean.
- Whole-branch review (fable) of `eaa7a2b..6ed9886`: With fixes, 0 Critical, 4 Important → single fix wave (`ec8067c`: plain-mode `/history <arg>` delegation, parser-derived `--json` message, `(archived)` suffix in shell listing, redact-before-slice in the fixture, `=`-exact `has-session`, launcher 409 wording `--agent-name`) → re-review clean.
- Controller verification on `ec8067c`: 528 tests OK, 2 expected skips, `git diff --check` clean, no leftover test children.
- Records: CLI ledger `.superpowers/sdd/2026-09-12-terminal-sessions-cli/progress.md`; `final-review-ledger-extract.md` lists every ruling and deferred minor; `final-fix-report.md` has the wave evidence. Workspaces kept on purpose until the user has read the report.

If you are a new session: do NOT re-dispatch anything. Read the last 10 lines of the CLI ledger, then act only on the user's integration choice (never merge/push without it).

### §8 addendum — TUI slice started (2026-09-13 00:2x IST)
User chose a full-screen TUI (told Codex directly). Codex forked `feature/terminal-tui` from `ec8067c`; the checkout is now on that branch. `feature/terminal-sessions` is untouched at `ec8067c` and its integration menu is still unanswered. Next gate: Codex posts `TUI design ready: <path>` → Claude dispatches a design review against the 9 criteria in the 00:2x `claude → codex` entry (spec §5 literals, security invariants, controller as single state source, attach/suspend/repaint, keyboard model, cell-level sanitisation, headless test strategy, non-goals, `--plain` fallback) → `Design approved` or revision list → then plan → new SDD workspace `.superpowers/sdd/2026-09-13-terminal-tui/`.

## §9 — TUI slice complete (2026-09-13)
- feature/terminal-tui HEAD e16bb75 (stacked on feature/terminal-sessions ec8067c). Tasks 1–12 clean; whole-branch review (fable) "With fixes" → ONE fix wave e16bb75 (R-FW1 /history, R-FW2 wheel, fetch supervision, empty providers + R-FW3 Windows precedence, README, spec §8) → scoped re-review all ADDRESSED → isolated full suite 906 OK.
- Deferred follow-ups: see TUI progress.md final entries.
- Pending: user integration decision (merge locally / push+PR / keep) for both stacked branches; origin bcurts/agentchattr ownership unconfirmed. Nobody merges or pushes without it.
