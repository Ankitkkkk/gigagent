# TUI server restart implementation plan

> **For agentic workers:** Use subagent-driven-development with bounded ownership and review.

**Goal:** Restart the connected main server from a visible TUI button while preserving agent terminals and drafts.
**Architecture:** Server-owned lifecycle with authenticated, instance-fenced restart; one-shot CLI request plus fresh-instance readiness polling; existing TUI action ownership and modal focus restoration.
**Tech Stack:** Python, FastAPI, uvicorn, prompt-toolkit, unittest.
**Spec:** docs/superpowers/specs/2026-09-22-tui-server-restart-design.md

## Constraints
Preserve existing uncommitted work. No live process actions. Test with isolated temporary instances and inert providers. Keep HTTP origin and session-token authentication. POSIX localhost run.py only; unsupported entrypoints fail before mutation.

## Task 1: Owned server lifecycle (root)
- [x] Add failing tests in tests/test_server_lifecycle.py and tests/test_server_restart_api.py for instance fencing, duplicate coalescing, auth, worker drain and exec invocation.
- [x] Implement server_lifecycle.py; wire run.py owned MCP/web servers and app.py restart routes/background cancellation. API GET /api/server returns instance_id, previous_instance_id, state (ready/starting/restarting), restart_supported, reason. POST /api/server/restart body is {instance_id: str}, 202 accepted with state restarting. After attempting the response (including send failure), stop transports/periodic work and drain before execv.
- [x] Run focused unittest regressions, independently review via AGENTS_CONVO.md.

## Task 2: TUI and client (delegated)
- [x] Add failing tests in tests/test_cli_tui_restart.py for visible compact button, default-No, confirmed payload, unsupported/stale/error and preserved draft/mode/selection. Add tests/test_cli_server_restart.py for old-server preflight, one POST, fresh token and new ready instance.
- [x] Implement WorkspaceAPI.server_status() and restart_server(instance_id, timeout=30), controller restart_server action {instance_id, confirmed}, and dialog/button/F4 entry. Do not retry mutations; after POST uncertainty continue only safe GET polling. Use fresh tokens for restart polls, never claim success from the old instance.
- [x] Run focused UI/API tests. Update README with location, preservation, first-upgrade manual restart requirement and timeout recovery.

## Task 3: Integration and final verification (root)
- [x] Add isolated real run.py restart integration using private ports/data/uploads/tmux and inert provider commands; prove new ready boot, same PID, token rotation, retained messages/workspace and agent terminal, WebSocket reconnect.
- [x] Request external review through AGENTS_CONVO.md and fix verified findings.
- [x] Run full unittest discovery under required outer isolation; report actual results and platform limits. Leave work uncommitted.
