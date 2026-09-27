# Agent profiles and orchestrator implementation plan

Spec: ../specs/2026-09-22-agent-profiles-orchestrator-design.md

## Constraints

Continue the existing feature/terminal-tui checkout and preserve the verified,
uncommitted UI/Stop-all work. No commit or push in this slice. Tests use temporary
stores, inert providers, and private tmux environments. Root owns integration;
file ownership below prevents concurrent edits. External codex-listener reviews
through AGENTS_CONVO.md, which remains local.

## Task 1 — profile persistence and lifecycle

Files: agent_profiles.py, workspace_store.py, workspace_launcher.py and focused
new profile/lifecycle tests. First add failing contract tests, then implement:

- `make_profile(role='generalist', personality='pragmatic') -> dict`, catalog
  constants ROLE_CHOICES/PERSONALITY_CHOICES, `profile_prompt(agent) -> str`.
- Store add_agent accepts profile/kind, validates snapshots, freezes updates,
  retains identity recovery. Orchestrator insertion atomically claims singleton.
- `set_orchestrator(ws_id, **fields)` updates persisted control state; add_agent
  orchestrator sets agent_id/enabled. Archive/remove disable/remove ownership.
- Launcher spawn accepts role/personality keywords and private kind; bootstrap
  contains frozen profile. `configure_orchestrator(ws_id, provider, cwd,
  provider_args=None) -> workspace` creates/resumes singleton under lifecycle lock.
- Explicit stop pauses manager before cleanup; resume enables it. Tick performs
  bounded verified recovery. Startup unread delivery includes new profiled agents.
- `startup_ready(name) -> bool` gates managed agents until startup done (unmanaged
  true). `on_startup_ready` optional callback wakes pending routing after bootstrap.

## Task 2 — terminal and shell controls

Files: cli.py, cli_workspaces.py, cli_workspace_chat.py, cli_tui_dialogs.py,
cli_tui_view.py, README.md and focused terminal tests. First capture form/action
contracts, then add staged role/personality spawn selection, locked resume
display, New session orchestrator provider/cwd choice, existing-session
orchestrator action, role/kind badges. API create accepts optional orchestrator
dict {provider,cwd,provider_args}; configure action POSTs /orchestrator.
Spawn accepts role/personality strings; backend returns profile snapshot/kind.
Preserve legacy callers omitting new fields, vim focus and draft behavior.

## Task 3 — routing service and server integration (root)

Files: orchestration.py, app.py, mcp_bridge.py, run.py, static/chat.js and focused
new tests. Build durable request ledger and token-owned read/dispatch service.
`chat_orchestrate(action='pending'|'route', message_id=..., agent_ids=[...],
reason=...)` uses authenticated current manager identity, never claimed sender.
Route only original ordinary unmentioned human messages before default fanout.
Use server validated stable recipients and existing unread records. Store
reservations before enqueue; reject stale originals and changed decisions.
Startup callback and tick notify pending work only after manager startup.
Role GET overlays saved profiles; edits return locked error and browser honors it.
Create/configure endpoints validate input and preserve useful failure state.

## Task 4 — integration and independent review

Run focused suites per task; review diffs against spec and close findings.
Exercise real isolated server/MCP with inert manager/worker and terminal forms,
including queued-before-ready, restart, stopped-manager, and Stop-all behavior.
Run full outer-isolated unittest discovery, report actual skips and limits.
Update README usage and AGENTS_CONVO.md with evidence. Leave watcher running.
