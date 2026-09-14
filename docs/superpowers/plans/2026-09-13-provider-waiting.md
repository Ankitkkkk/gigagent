# Provider waiting signals

User scope: extensible waiting detection, Codex first, informed by the supplied
MCP permission screenshot and PermissionRequest hook. This extends the current
boolean heartbeat/Attach hint. It never answers an approval.

## Design and implementation

1. Move generic terminal parsing behind ProviderAdapter.waiting_for_input.
   Codex overrides it for its MCP Allow/Cancel modal, including enter-to-submit.
   The Unix activity checker uses the configured adapter (including plugins),
   reusing its existing capture. Tests cover the supplied screenshot text,
   resolved prompts, custom adapters, and existing generic behavior.
2. Add a normalized provider hook event interface. Codex recognizes
   PermissionRequest, PostToolUse, Stop, Interrupt and SessionEnd. Hooks write
   small atomic event files into a private directory belonging to one wrapper
   launch. Environment supplies that directory only to the wrapped provider.
   No tokens, tool inputs, descriptions or transcript content are recorded.
   Tests run the real hook command with synthetic stdin and concurrent writers.
3. A wrapper monitor consumes bounded batches. PermissionRequest sets a hint;
   matching PostToolUse or turn/session termination clears it. Visible prompt
   dismissal also clears it, since tools can run long after approval. Events
   lacking exact call IDs are conservative per session/turn/tool, not a full
   approval protocol. Uncorroborated hook hints expire after 60 seconds; the
   existing server lease still handles missing wrappers. Terminal capture
   remains the fallback for hooks awaiting trust and unsupported modals.
4. Provide `python waiting_hooks.py install --provider codex --project PATH`.
   Preserve existing project hook entries; install one stable command per event
   idempotently and atomically. The command is inert outside managed wrappers.
   Never overwrite global Codex config or bypass native hook trust. Invalid
   config causes a clear error with no write. Document `/hooks` review and
   wrapper relaunch requirements. Install into this project after verification.

## Verification

Write RED tests before implementation, then run focused provider/monitor/hook/
wrapper tests. Run the outer-isolated full suite for wrapper integration.
Use synthetic hooks and panes, no paid provider invocation or approval input.
Keep Claude informed via AGENT_MESSAGES.md. Existing uncommitted hint changes
and user venv remain intact. Do not restart a live pending approval merely to
exercise the implementation.

## Source

https://learn.chatgpt.com/docs/hooks (checked 2026-09-13): PermissionRequest
includes MCP names; PostToolUse has session/turn/tool identity; hooks require
native trust; returning no decision preserves ordinary approval flow.
Installed CLI is 0.154.0 and contains PermissionRequest hook support.
