# Saved agent profiles and session orchestration

## User contract

New workers choose a role and personality at creation. Both remain fixed for the
saved agent, including stop/resume and fresh conversations. Presets supply actual
startup instructions, not only badges. Roles: generalist, implementer,
code-reviewer, planner, tester, debugger. Personalities: pragmatic, meticulous,
concise, supportive. A snapshot of the expanded instructions and catalog version
is stored so catalog updates cannot silently change existing agents.

New terminal sessions choose an orchestrator provider at creation. The
orchestrator is a distinct saved agent, not a worker role. It remains resident
and waits for requests in its non-archived, enabled session. This scope is server
state, independent of which session a client selects. Legacy sessions remain
unchanged until the user enables orchestration through session actions.

Only new ordinary human channel messages without an explicit @handle enter
orchestration. Explicit mentions (including @all/@both and unknown handles)
bypass it. Emails are not handles. Agent replies, system messages, summaries,
jobs, structured workflow turns, and imported/replayed history retain existing
behavior. Group/default broadcasts exclude the orchestrator unless directly
mentioned by its full saved name.

The orchestrator reads a server-provided pending request and current worker
roster, then chooses one or more workers by name, role, and personality. It only
routes; it does not implement the task. A token-authenticated MCP tool validates
its saved ownership and dispatches the original message. A system notice shows
the assignment. No separate addressed relay is sent, avoiding duplicate routes.

## Persistence and delivery

Each saved agent has immutable `kind` (worker/orchestrator) and `profile` fields.
Legacy records without them behave as neutral workers. Both normal and
launch-conditional update paths reject changes; identity recovery preserves them.
The legacy mutable role API cannot overwrite a saved profile. Structured workflow
roles remain temporary task assignments.

Workspace `orchestrator` state contains `agent_id`, `enabled`, and supervision
retry metadata. Creation is serialized with launch/stop and persists ownership
before starting the wrapper. Stop, Stop all, archive, and removal pause supervision
before cleanup; human messages while paused stay pending and do not resume it.
Explicit Resume enables it again. Crash recovery verifies resource absence and
uses bounded retry/backoff; uncertain process probes never justify duplicates.

Startup queues are gated until the identity/profile prompt has been enqueued.
Requests arriving earlier remain in persisted unread routing (workers) or the
orchestration ledger (manager), then wake after startup. No pre-wrapper queue is
used because wrappers clear that queue at launch.

An atomic JSON request ledger is keyed by workspace and original message ID.
Pending requests survive restart and are re-notified on a new manager launch.
Decisions store original message content, selected stable worker IDs, and per-target
enqueue outcomes before transport. Repeated identical decisions are idempotent;
changed decisions are rejected. Edited/deleted originals cannot be dispatched.
Only current, ready, same-session workers are selectable, with structured turn
guards checked again at dispatch. No eligible worker leaves the request pending.

New ingress records server-owned actor kind so a human display name cannot turn
into an agent identity and a renamed agent cannot turn into a human sender.
Dispatch carries stable saved IDs, then verifies current name/token ownership
through queue append. History floor and audience predicates apply to both
orchestrator context and selected workers. Profile-aware clients check server
capabilities before new mutations to prevent silent downgrade on an old server.

The JSONL wrapper transport cannot promise exactly-once consumption. A crash
between reservation and enqueue is explicitly uncertain; it is never blindly
replayed. Saved unread messages and the existing Retry action allow deliberate
recovery. Successful enqueue is reported as queued, never as proof of execution.
Late assignments have explicit unacknowledged IDs independent of monotonic read
cursors. Assignment tombstones make ledger recovery idempotent after an ACK and
routing-table pruning. Recovery restores unread state without replaying transport.

## UI and compatibility

Add agent uses a compact staged profile chooser; Resume displays the locked
profile without edit controls. Agent rows show the role and distinguish the
orchestrator. New session selects provider and working directory; existing
sessions have an Enable orchestrator action. Server errors remain visible and
forms retain values. Plain shell/API creation without orchestration remains
backward compatible; optional role/personality and orchestrator fields provide
the same capability outside the TUI. Terminal forms must fit 80x18.

## Acceptance

Verify immutable persistence, catalog snapshots, recovery shadows, startup order,
explicit/default/unknown routing, no recursive routing, authenticated ownership,
cross-session rejection, duplicate and stale decisions, queued/uncertain states,
manager recovery and Stop all fences. Exercise TUI forms and real isolated
HTTP/MCP routing with inert providers. Run the full suite in its outer isolated
environment. No live user agents, paid provider calls, or real history in tests.
