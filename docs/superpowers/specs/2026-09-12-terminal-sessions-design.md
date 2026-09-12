# Terminal Sessions: Design

Date: 2026-09-12
Status: In progress. Sections are appended as they are approved in conversation.
Supersedes the open questions in `SPAWN_SESSION_HANDOVER.md`.

Terminology: the user-facing word is **session**. In code, the concept is
**workspace** (`workspace_store.py`, `WorkspaceStore`, `/api/workspaces`) to avoid
colliding with the existing structured-workflow `session_store.py` and
`/api/sessions/*`.

## Decisions

| # | Decision | Chosen |
|---|----------|--------|
| D1 | Picker entry represents | One workspace: project dir + dedicated channel + its agents. |
| D2 | Workspace ↔ channel | 1:1. Creating a workspace creates its channel. |
| D3 | History on first join | Three modes: `none`, `literal`, `summary`. Enforced by a per-agent, per-channel visibility policy (floor + audience) applied in every MCP tool that returns messages. State is keyed by stable `agent_id` in the workspace record. Choice is one-way: `none → literal` later is fine; `literal → none` cannot un-tell the provider. |
| D4 | Summary mode | Headless light model summarises the channel. The summary is posted into the channel as a message that humans and the joining agent can see; other agents never can (audience-restricted in the MCP read path). Floor stays at join time. On failure there is no automatic fallback: the user chooses retry, literal, or none. |
| D5 | Acknowledgement | A routed message is acknowledged only when a read tool (`chat_read`, `chat_resync`) actually returns it. Per-message acks are kept above a low-water `read_mark`; the mark advances only when every routed message up to it is acked. Sending never acknowledges; a read that skips messages never acknowledges the skipped ones. |
| D6 | Unread and delivery | Unread = messages routed to the agent (per `router.py`) past its cursor. On resume, all unread messages are delivered in one trigger; when there is more than one it ends with a "you missed N messages" marker. `/retry <agent>` re-sends the same bundle to a running agent. |
| D7 | Server start | CLI auto-starts `run.py` in a background tmux session if the configured port is down. Reuses it if up. One config = one server. |
| D8 | Stop / quit | Stop kills the agent's tmux; wrapper deregisters; the record keeps native id and cwd. Quitting the chat stops nothing but checkpoints every running agent: native id (re)discovered through its adapter, transcript verified, cwd confirmed. Restart re-creates tmux from the record; tmux names are never stored. |
| D9 | Delete | Never. Archive stops the agents and hides the entry from the active list; the record, its channel messages, floors, native ids and cwds are all kept. Unarchive puts it back in the picker and agents resume as usual. |
| D10 | Ownership | Approach A: server owns records and launches wrappers. CLI is a thin client. Wrapper gains `--no-attach`, `--cwd`, `--identity-file`. |
| D11 | Record location | Inside the server `data_dir`, next to the channel store it references. |
| D12 | Platform v1 | Linux and macOS (tmux). Windows chat works; spawn fails with a clear message. |
| D13 | Agent role | Deferred to a later phase. |
| D14 | Provider specifics | Behind a `ProviderAdapter` interface in `providers/`. v1 ships claude and codex adapters plus the `adapter = "module:Class"` hook for user-written ones. Config-only `generic` adapter is phase 2. |
| D15 | Selecting a session | Shows agent status, asks once `Resume N stopped agents? [Y/n]`, resumes all that can be and reports each that cannot. `--no-resume` answers no for scripts. |

Assumptions stated and not contradicted: names need not be unique (id is); the
picker shows an id suffix on duplicates. If a recorded `cwd` no longer exists,
the picker warns and resume is blocked until the user re-points it.

## 1. Data model

`data/workspaces.json`, written atomically (temp + rename) by the server only.

```json
{
  "workspaces": [{
    "id": "ws_7f3a…",
    "name": "billing refactor",
    "channel": "ws-billing-refactor-7f3a",
    "archived": false,
    "created_at": "2026-09-12T10:00:00Z",
    "updated_at": "2026-09-12T10:42:00Z",
    "agents": [{
      "agent_id": "ag_1c9e…",
      "provider": "claude",
      "registry_name": "claude-1",
      "cwd": "/abs/path/to/project",
      "native_session_id": "uuid-or-null",
      "previous_native_ids": [],
      "previous_cwds": [],
      "last_launch": {"kind": "spawn | resume | fresh", "nonce": "uuid", "at": "…", "pid": 12345},
      "history_mode": "none | literal | summary",
      "history_state": "pending | done | failed",
      "history_note": null,
      "floor_id": 101,
      "read_mark": 140,
      "acked_above_mark": [143, 147],
      "native_verified": false,
      "joined_at": "2026-09-12T10:05:00Z",
      "last_state": "starting | running | exited | unknown",
      "last_error": null
    }]
  }]
}
```

- `id` — tool-owned, stable, uuid4 with `ws_` prefix.
- `name` — user-defined, editable, defaults to `id`.
- `channel` — created with the workspace and registered as a real channel in
  the room settings: `ws-` + the first 11 characters of the name's slug + `-`
  + the first four hex characters of `id` (20 characters, the channel-name
  limit), so two workspaces with the same name never share a channel.
  Renaming the workspace does not rename the channel.
- `agent_id` — stable handle for one agent membership. API routes use it, not
  `registry_name`, because the registry may rename an instance (slot-1 rename
  when a second instance of the same base registers) or assign a different slot
  on resume.
- `registry_name` — the current registry identity for mentions and auth.
  Default convention is `<provider>-<n>` with `n` incremental per provider
  (`claude-1`, `claude-2`, …), never a bare `claude`. The user may pass a
  custom name at spawn (`--name reviewer`); the registry enforces uniqueness
  across families. Updated by the store when the registry renames or when
  resume lands on a different name.
- `native_session_id` — provider conversation id. `null` means not yet captured
  (see §6). Resume refuses to run on `null` unless the user explicitly asks for a
  fresh conversation.
- `updated_at` — bumped on create, rename, join, resume, stop. The picker sorts
  on it, newest first.
- `floor_id` — first message id this agent may see in the workspace channel
  (inclusive). `read_mark` and `acked_above_mark` — acknowledgement state,
  defined under "Visibility policy". All keyed by `agent_id` here, not by
  registry name in `mcp_bridge`, so they survive deregister
  (`purge_identity`), rename and resume under a new name.
- `history_state`, `history_note`, `native_verified`, `last_error` — status
  fields written by the launcher and summariser (§3, §6, §7).

The five identifiers stay separate: workspace `id`, display `name`,
`registry_name`, tmux session name (`agentchattr-<agent_id>`, derived from
the stable id so renames never strand `/attach`), and `native_session_id`.

**Floor.** see "Visibility policy" below.

### Visibility policy

One predicate, applied everywhere an agent receives channel messages:

```
visible(agent, msg) =
    (agent is not a member of msg.channel's workspace
        or msg.id >= floor_id(agent, msg.channel))
    and (msg.metadata.audience is absent or agent.agent_id in it)
```

The audience clause binds every agent, member or not: a non-member has no
`agent_id` in any audience and therefore never sees a private summary. The
floor binds members only.

- **Floor.** Message ids start at 0 (`store.py:17`), so the floor is
  inclusive: `literal` → `floor_id = 0`; `none` and `summary` →
  `floor_id = latest id in channel + 1` (0 if the channel is empty).
- **Where it lives.** `floor_id` and `read_mark` sit on the agent entry in
  `workspaces.json`, keyed by `agent_id`. `mcp_bridge` does not own them.
  `run.py` registers a `workspace_policy(registry_name, channel)` hook that
  returns `(agent_id, floor_id)` for a workspace agent and `None` for any
  other caller. Non-workspace agents are unaffected.
- **Where it is applied.** `chat_read` (first read, cursor read, explicit
  `since_id`), `chat_resync`, `chat_summary(read)`, and any job-thread read
  that includes channel messages. One helper, `_apply_visibility(sender,
  msgs)`, called at the end of each, so a new read tool cannot forget it.
- **Fail closed.** The hook classifies a caller as a workspace agent if
  its `registry_name` appears in the store **or** in any
  `data/identity/*.json` shadow. A workspace agent with no `floor_id` in
  the store and none in its shadow gets only "history policy unavailable
  for this channel; ask the user to run /history <agent> <mode>" and no
  messages. If the shadow has a `floor_id`, it is used. `/history` writes a
  floor and normal reads resume. A `none` choice is never silently widened
  by data loss, and a lost store never turns a managed agent into an
  unrestricted one.
- **Acknowledgement state.** Two fields, keyed by `agent_id`:
  `read_mark` — every message routed to this agent with `id <= read_mark`
  is acknowledged; `acked_above_mark` — ids above the mark that a read
  tool has returned. When `chat_read` or `chat_resync` returns messages,
  each returned id that is routed to the agent is added to
  `acked_above_mark`; then the mark is compacted: while the smallest routed
  id above `read_mark` is in the set, move the mark past it and drop it.
  Ids the read did not return are never acknowledged — a cursor read that
  starts after the agent's own send, or a first read that shows only the
  newest `limit`, leaves earlier routed ids unread. `chat_send` and
  `chat_propose_job` keep advancing the existing `_cursors` (that is what
  makes the next `chat_read` skip the agent's own message) but never touch
  either field. The set stays small because it only ever holds routed ids
  that were read out of order.
- **HTTP read path.** The security middleware (`app.py:239-243`) lets a
  registered agent call `GET /api/messages` with its bearer token. That is
  a supported agent read path, so it applies the same predicate: when the
  request carries an agent token, the response is filtered through
  `visible()` for that agent (fail-closed rule included). Browser requests
  with the session token are unchanged. `GET /api/export` returns 403 to an
  agent bearer token (it is a browser-only bulk download).
- **Out of scope, stated.** An agent with shell access can fetch the
  browser session token from `/` on loopback, or read
  `data/messages.jsonl` directly. The policy governs the agent-facing read
  paths; it is not a sandbox.

## 2. Server: store, API, launcher

### Modules

- `workspace_store.py` — `WorkspaceStore(path)`. Load, atomic save, CRUD,
  archive/unarchive, `list()` sorted by `updated_at` desc, `on_change` callback
  (for a later WebSocket broadcast). Knows nothing about processes.
- `workspace_launcher.py` — spawn, resume, stop, reconcile. Talks to the
  registry, tmux, config and the filesystem. Owns no persistent state; writes
  results back through the store.
- `app.py` — thin routes under `/api/workspaces`.

### Routes

| Method | Path | Body / query | Result |
|---|---|---|---|
| GET | `/api/workspaces` | `?include_archived=0` | list; each agent carries live `state` and `unread_count` |
| POST | `/api/workspaces` | `{name?}` | created record |
| PATCH | `/api/workspaces/{id}` | `{name}` | updated record |
| POST | `/api/workspaces/{id}/archive` | | stops agents, sets `archived` |
| POST | `/api/workspaces/{id}/unarchive` | | clears `archived` |
| POST | `/api/workspaces/{id}/agents` | `{provider, cwd, history_mode, name?}` | new agent entry |
| POST | `/api/workspaces/{id}/agents/{agent_id}/resume` | `{fresh?, name?, cwd?}` | agent entry |
| POST | `/api/workspaces/{id}/agents/{agent_id}/stop` | | agent entry |
| POST | `/api/workspaces/{id}/agents/{agent_id}/history` | `{mode}` | change or resolve history mode; rules in §3 |
| GET | `/api/workspaces/{id}/unread` | `?agent_id=` | unread messages (§4) |
| POST | `/api/workspaces/{id}/agents/{agent_id}/retry` | | re-sends the unread bundle (§4) |
| POST | `/api/workspaces/{id}/checkpoint` | | records native ids and verifies transcripts for running agents |

All validation errors return 400 with a plain-English `error` field; the CLI
prints it verbatim.

### Spawn

1. **Validate before any side effect**: workspace exists and is not archived;
   `provider` is a key in `[agents]`; `cwd` is absolute, exists, is a directory;
   the provider `command` is on `PATH`; `tmux` is on `PATH`.
2. **Identity**: `registry.register(provider, label=f"{workspace.name} {provider}",
   preferred_name=<custom name or f"{provider}-{next free n}">)`. "Next free"
   skips names held by any saved workspace agent, running or not, so a stopped
   agent's name is never handed to a new spawn and lookups by name stay
   unambiguous. `preferred_name` is a small registry addition: take that name
   if its slot is free, else 400 `name in use` for a custom name (also when a
   saved agent holds it). Returns `registry_name` and token; the
   server writes `data/identity/<agent_id>.json` holding `registry_name`,
   token, `workspace_id`, `agent_id`, `channel`, `history_mode`, `floor_id`
   and `last_launch` — the policy shadow used when the workspace store
   cannot be read (§7).
3. **Floor**: per the visibility policy in §1, inclusive. `literal` →
   `floor_id = 0`; `none` and `summary` → `floor_id = (latest id in the
   channel) + 1`, or 0 if the channel is empty. `store.last_id()` is global
   and is not used. Stored on the entry and in the identity file.
4. **Native id**: `adapter.allocate_session_id()` (§6). Claude returns a
   fresh uuid4 now; codex returns `None` and the id is discovered after
   launch.
5. **Persist** the agent entry with `last_state: "starting"` and
   `history_state`: `done` for `none` (nothing to do), `pending` for
   `literal` and `summary` (becomes `done` when the catch-up trigger is
   enqueued, or when the summary is posted; `failed` per §3).
6. **Launch** the wrapper as a detached child of the server
   (`subprocess.Popen(..., start_new_session=True)`), stdout and stderr to
   `data/logs/wrapper-<agent_id>.log`:

   ```
   python wrapper.py <provider> --no-attach --no-restart \
       --cwd <cwd> --identity-file data/identity/<agent_id>.json \
       --tmux-name agentchattr-<agent_id> \
       -- <adapter.new_session_args(id)>
   ```

   The wrapper still creates the provider's tmux session exactly as today,
   under the stable name it was given; nothing is nested.
7. **Ready**: today the wrapper's heartbeat thread starts before
   `run_agent` (`wrapper.py:796` vs `:917`), so a first heartbeat proves
   only that the wrapper is up. The heartbeat body gains `ready: true`,
   set once the provider tmux session exists and its pane has produced
   output (the activity checker's first observation). `last_state` becomes
   `running` on the first heartbeat with `ready`; everything in step 8, the
   codex id discovery (§6) and the unread bundle (§4) wait for it. Spike
   (2026-09-12): a claude pane shows output about 2 s after `new-session`.
   Known limitation: in a directory Claude has never seen, that first output
   is its trust prompt ("Is this a project you trust?"); the agent counts as
   `running` but acts on nothing until the user answers it via `/attach`.
   The CLI's `/spawn` output says so when the cwd has no `.claude` state.
   No `ready` within 60 s of launch → the launcher **terminates that launch**:
   `tmux kill-session -t agentchattr-<agent_id>` if it exists, `SIGTERM` to
   the wrapper pid from the identity file (then `SIGKILL` after 5 s),
   `registry.deregister(registry_name)`, identity file kept (shadow). Only
   then `last_state: exited`, `last_error` from the log. Nothing is left
   that could hold the name or the tmux session against a later resume.
8. **History**: per §3. `literal` → catch-up trigger. `summary` → generate,
   post the summary card, then a direct trigger. `none` → nothing. All of it
   runs only after step 7, never before.

### Wrapper changes

- `--cwd PATH` overrides `agent_cfg["cwd"]`. Both the MCP config written for
  the provider and the launched process use the same resolved path.
- `--identity-file PATH` reads `registry_name` and token and skips
  `/api/register`. Existing behaviour is unchanged when the flag is absent.
- `--no-attach` skips `tmux attach-session` and goes straight to the keep-alive
  loop that today runs after a manual detach.
- `--tmux-name NAME` overrides the default `agentchattr-<agent>` session name
  passed to `run_agent`.
- Heartbeats carry `ready: bool` (see Spawn step 7).
- `--no-restart` already exists; managed agents always pass it so an exit is an
  exit and resume is explicit.

### Resume

1. Validate: the effective `cwd` (`cwd` from the request if given, else the
   stored one) is absolute, exists and is a directory; command on `PATH`;
   agent is not already running (tmux session absent and registry name not
   active); `native_session_id` is not `null`, or the request says
   `fresh: true`; `name` from the request, if given, passes the registry's
   family-conflict check and is not held by a live instance.
2. Identity: register with `preferred_name` = `name` from the request if
   given, else the stored `registry_name`. Taken by a live instance → 409
   (§7) naming the `--name` option. On success the new `registry_name` is
   persisted; `agent_id`, floor, `read_mark` and acks live on the record, so
   nothing needs migrating; the registry cursor starts fresh, which only
   affects what a cursor-less first `chat_read` returns.
   A `cwd` from the request is persisted before launch (the old one goes to
   `previous_cwds` for audit). For codex the new directory is passed with
   `-C`; for claude the transcript pre-check is cwd-independent, and the
   spike (2026-09-12) confirmed `claude --resume <id>` from a different
   directory continues the conversation, so `--cwd` is allowed for claude.
3. Launch as in Spawn step 6 with `adapter.resume_args(id, cwd)` in place of
   the new-session args. Codex returns a leading subcommand, so
   `_build_provider_launch` must accept positional args before flags.
4. No history injection. Once `running`, the unread bundle is delivered
   per §4.

### Fresh (`{fresh: true}`)

Same validation as resume except that `native_session_id` may be null and
the adapter need not support resume. Then:

1. `adapter.allocate_session_id()` and `adapter.new_session_args(id)` as in
   Spawn steps 4 and 6; codex discovery runs again. The previous id, if
   any, is appended to `previous_native_ids` on the entry for audit; it is
   never deleted from the provider.
2. `agent_id`, `registry_name`, `cwd`, `floor_id`, `read_mark`,
   `acked_above_mark` and `history_mode` are unchanged.
3. Because the new conversation has none of the old one's context, the
   history injection of §3 runs again according to the current
   `history_mode` (`none` → nothing; `literal` → catch-up trigger;
   `summary` → a new private summary). Then the unread bundle (§4).
4. The entry's `last_launch.kind` is `"fresh"` (else `"spawn"` or
   `"resume"`); `last_state` goes through the normal `starting → running`
   lifecycle. The picker line shows `fresh` while `kind = fresh` and the
   state is `starting`, so a fresh start is never mistaken for a resume,
   and reconciliation needs no special case.

### Stop

`tmux kill-session -t agentchattr-<agent_id>`. The wrapper sees the
session die, deregisters through the existing path, and exits because of
`--no-restart`. The deregister handler in `app.py` marks the agent
`exited`. If the wrapper process outlives the tmux session by more than a few
seconds, the launcher sends it `SIGTERM` using the pid recorded in the identity
file.

### Checkpoint

`POST /api/workspaces/{id}/checkpoint` runs for every running agent in the
workspace: if `native_session_id` is null, call the adapter's
`discover_session_id` again with the persisted `last_launch` context; call
`locate_transcript` and set
`native_verified`; confirm `cwd` still exists; bump `updated_at`. Idempotent
and cheap. The CLI calls it on `/quit`, on `/sessions`, and before `/archive`;
the launcher calls it before `stop`. This is what makes "pick up where we
left off" depend only on the record — never on a tmux session surviving.

### Reconcile on server start

`last_state` has exactly four values — `starting`, `running`, `exited`,
`unknown` — for every launch kind. For every non-archived agent with
`last_state` in `starting|running`: if `tmux has-session` succeeds, keep the
state (a `starting` one continues to wait for `ready` under the same 60 s
rule, counted from server start) and, if `native_session_id` is null,
re-run `discover_session_id` with the persisted `last_launch`; else set
`exited`. A fresh launch interrupted by a reboot therefore lands in
`exited` like any other and appears in the picker's stopped-agent resume
question. This makes the picker truthful after a reboot.

### Registry rename hook

Where `app.py` already calls `mcp_bridge.migrate_identity(old, new)` on a
rename or slot-1 rename, it also calls `workspace_store.rename_agent(old, new)`.

## 3. History modes

Chosen once when an agent joins. Enforced by the floor (§1), not by the prompt.

| Mode | `floor_id` at join | What the agent gets |
|---|---|---|
| `none` | latest id + 1 | Nothing before join. Messages after join flow normally. |
| `literal` | 0 | A catch-up trigger. The agent pages through real history with `chat_read`. |
| `summary` | latest id + 1 | A generated summary, posted into the channel as a `summary` message, then a trigger. Raw history stays below the floor. |

### Floor enforcement in `chat_read`

The single `visible()` predicate in §1 (`msg.id >= floor_id`, inclusive) is
the only rule. No section defines a second one.

### Injection timing

Nothing is enqueued until the agent's first heartbeat flips it to `running`,
because `wrapper.py` clears the queue file at startup. The literal trigger and
the summary trigger are both written after that transition.

### Literal

`chat_read` today truncates to the newest `limit` even for an explicit
`since_id` (`msgs[-limit:]`), and treats `since_id=0` as "not given", so an
agent cannot page forward from message 0. Change: `since_id: int | None =
None`; when given (any value, including 0 or −1), return the oldest `limit`
messages after it and append a final line `has_more: true, next_since_id: N`
when more remain. Cursor reads and first reads are unchanged.

Queue entry with the custom prompt:

> Catch up: use mcp to read #<channel> with since_id=-1 and keep reading
> while has_more is true, then respond in #<channel> with a two-line status
> of where things stand.

The cap is the agent's own paging; the server does not truncate.

### Summary

`workspace_summarizer.py`, run in a background thread. The spawn response
returns immediately with `history_state: "pending"`; it becomes `done` or
`failed`.

1. **Reuse first.** If the channel has a stored summary (`summaries.get`)
   whose `message_id` is within 10 messages of the current latest id, its
   text is reused and no headless call is made. That summary was written by
   an agent for everyone, so reusing it leaks nothing.
2. **Input.** Channel messages of type `chat` and `summary` that the joining
   agent would be allowed to see if the floor were 0, oldest first, capped at
   the most recent 300 messages or 40 000 characters, whichever is hit first.
   If anything was dropped, the prompt says "earlier messages omitted".
   `join`, `leave`, `system` and draft types are excluded.
3. **Provider.** `[workspace] summarizer = "auto" | "<provider>"` in
   `config.toml`, default `auto`. The command comes from the provider
   adapter's `summarizer_command` (§6); `auto` picks the first adapter that
   offers one and whose `command` is on `PATH`, claude before codex, then
   config order. `summarizer_model` per provider, default `haiku` for claude
   and the provider's own default otherwise. **Isolation requirement:** the
   summariser must have no tools at all — no shell, no file reads, no MCP —
   because the chat text is untrusted input and a prompt-injected "read
   ~/.ssh/id_rsa" must be impossible, not merely sandboxed. `cwd` is a fresh
   temporary directory in every case; the prompt wraps the chat in a
   delimited block and says to summarise, not obey.
   - claude: `claude -p --model haiku --tools "" --strict-mcp-config
     --setting-sources "" --no-session-persistence`. Spike (2026-09-12): the
     flags are accepted, no MCP servers load, and a prompt-injected "read
     this file" left the canary untouched. Haiku *confabulated* a tool call
     and invented file contents instead — harmless, but the summariser prompt
     must state "you have no tools; do not pretend to use any" so the summary
     is not padded with invented reads. This is the v1 summariser.
   - codex: `exec -s read-only` only sandboxes shell commands; it does not
     remove them. Spike (2026-09-12, codex 0.154.0): given the same probe,
     codex attempted `cat` on the canary and was stopped only by a broken
     bubblewrap sandbox on that machine; `exec --help` exposes no tool-off
     switch (only generic `--enable/--disable <FEATURE>`). Codex's
     `summarizer_command` returns `None` in v1; `auto` never picks codex and
     `summarizer = "codex"` is a config error with that explanation.
   Timeout 90 s. The prompt asks for: decisions made, open questions, who is
   working on what, current state; plain text; under 900 characters.
4. **Post, private to the agent.**
   `store.add("system", text, msg_type="summary", channel=channel,
   metadata={"workspace_id", "agent_id", "audience": ["<agent_id>"],
   "generated_by": "<provider>/<model>"})`. It is **not** written to the
   channel-wide `SummaryStore`, which every agent can read through
   `chat_summary`. **Audience rule:** `chat_read` and `chat_summary` never
   return a message whose `metadata.audience` exists and does not contain the
   caller. Humans see it in the browser and CLI as a summary card labelled
   "for @<registry_name>". Then a queue entry for the agent with the prompt:
   "A private summary of #<channel> was posted for you. Use mcp to read
   #<channel>, then respond with a two-line status." Output longer than
   1000 characters is truncated at the last sentence boundary before the cap.
5. **Failure** — summarizer not on `PATH`, timeout, non-zero exit, empty
   output: **no automatic fallback.** `history_state: "failed"`,
   `history_note: "<reason>"`. The floor stays at join time and the agent
   receives nothing yet. A `system` message is posted: "Summary for
   @<registry_name> failed (<reason>). Choose: retry, literal, or none." The
   CLI prompts `[r]etry / [l]iteral / [n]one` when interactive; a shell
   command prints the hint `cli.py history <agent> --session <id> --mode …`.
   The agent stays usable meanwhile; only the catch-up is on hold.

The channel text is sent to the same provider the user already runs agents
with; no new third party is involved.

### Changing or resolving the mode

`POST /api/workspaces/{id}/agents/{agent_id}/history` with `{mode}`:

- `history_state = failed`: any mode. `summary` reruns generation; `literal`
  sets the floor to 0 and enqueues the catch-up trigger; `none` marks the
  state `done` and leaves the floor.
- `history_state = pending`: 409 "summary in progress".
- `history_state = done` (the state every `none` agent starts in, and
  every `literal`/`summary` agent reaches once its catch-up is enqueued or
  posted): `none → literal` sets the floor to 0 and enqueues the catch-up
  trigger; `none → summary` generates as above. `literal → none`,
  `summary → none`, `literal ↔ summary`: 400. The provider already holds what
  it read; a lower floor cannot be raised honestly. The error text says so.
  `history_state` is never null.

## 4. Unread, delivery on resume, retry

No new acknowledgement call. A routed message is acknowledged only when a
read tool (`chat_read`, `chat_resync`) returns it to the agent; it is then
added to `acked_above_mark` and `read_mark` compacts when contiguous (D5,
§1). Cursor movement and sending acknowledge nothing.

### Recording who a message was for

Every channel message — WebSocket send, MCP `chat_send`, `/api/send` — is
routed in one place: the store observer `_handle_new_message` in `app.py`,
which runs after the message is persisted. (The two job-thread routing
sites in `app.py` and `mcp_bridge.py` route job messages, which are not
channel messages and are outside the unread model.) Because the message is
already on disk when routing runs, recipients are not written into the
message; they are recorded in a side table in the workspace record,
`routing: {"<msg_id>": ["<agent_id>", …]}`, one entry per routed message
in a workspace channel, pruned when every member's `read_mark` has passed
the id. The workspace also keeps `routing_high_water` plus `routing_done`,
the processed ids above it; the mark advances only through ids contiguous in
that channel's own sequence, because observers can finish out of order. On
server start, every channel message above the mark that is not in
`routing_done` is replayed for explicit mentions. Broadcast recipients depend
on who was running at the time and are not reconstructed. Saved registry
names — archived records included — are never handed to a new agent. Recipients are computed by
`workspace_store.resolve_recipients(channel, mention_tokens, targets)`:
  every workspace member in that channel whose current `registry_name`
  matches an **explicit** mention token, plus every member whose
  `provider` matches an explicit family token (`@claude`), **including
  stopped agents** — explicit mentions are intent and survive a dead
  agent. For everything else — `@all`, `@both`, and a message with no
  mention at all — the recipients are **the router's actual targets for
  that message, intersected with running members**. With this repo's
  default `routing.default = "none"` (`config.toml:98`) a no-mention
  message has no targets and therefore no recipients; with `"all"` the
  router returns every name in the vocabulary (`router.py:64`) and the
  intersection keeps only running members. Stopped members are never
  recorded as recipients of a broadcast, so widening the router vocabulary
  does not create offline recipients. The router's mention regex is fed
  `configured provider names ∪ registry.get_all_names() ∪ workspace member
  names` through `router.update_agents` whenever any of them changes.
  `get_all_names()` holds instance names only (`registry.py:530`), so the
  provider names are what keep `@claude` parsing when only `claude-1` is
  registered, and the member names are what keep `@claude-2` parsing while
  claude-2 is stopped (today it would not: `app.py:753`). A routed name with
  no live instance is skipped by `agents.trigger` exactly as now.

`store.update_message` rewrites and fsyncs the whole JSONL and is not used
for this; the side table is why.

### Definition

For workspace agent A in channel C:

```
unread(A) = { m in C :
              visible(A, m)
              and m.id > A.read_mark
              and m.id not in A.acked_above_mark
              and A.agent_id in routing[m.id]
              and m.sender != A.registry_name
              and m.type in (chat, summary) }
```

Only a read that returns the message acknowledges it (D5). Unread 141–149,
agent sends 150, then reads 151: the read acks 151 into
`acked_above_mark`; the mark stays at 140 because 141 is not acked; 141–149
remain unread. Renaming A does not change `agent_id`, so earlier recipients
still match.

Broadcasts (`@all`, `@both`, and the no-mention path when
`routing.default = "all"`) record only running members as recipients, so a
broadcast sent while A was dead is not unread for A. With the default
`routing.default = "none"`, a no-mention message is routed to nobody and is
unread for nobody. Only explicit mentions survive a dead agent. This is a
v1 limitation and is stated in the CLI help.

### The bundle

One trigger carrying everything unread, sent through `agents.trigger` on the
same queue path as a live mention. Let the unread ids be `a … b`, N in total.

- N = 1: the normal mention prompt — "use mcp to read #C with since_id=a−1 —
  message #a from <sender> was sent to you and has not been read; act on it
  and respond in #C".
- N > 1: "While you were away, N messages were addressed to you in #C:
  #a (<sender>), #… , #b (<sender>). Use mcp to read #C with since_id=a−1 to
  see them in order, act on them, and respond in #C. — End of missed
  messages: N in total, nothing else is pending for you." The closing marker
  appears only when N > 1. Over 50 ids, the list shows the first and last
  five and "… and K more".

`since_id = a−1` returns oldest-first from `a` (§3 paging change) and every
id in the bundle is visible by construction. The agent's own `chat_read`
from that point acknowledges exactly the ids it returns; a bundle longer
than one page is cleared page by page as the agent follows `has_more`.
Nothing else marks anything read.

### On resume

When a resumed agent reaches `running` (heartbeat with `ready`) and
unread(A) > 0, the launcher sends the bundle once. This is the only automatic delivery on resume. Fresh spawns
never receive a bundle (nothing was routed to them yet); their catch-up is
the history mode (§3).

### Manual retry

`POST /api/workspaces/{id}/agents/{agent_id}/retry` (no body):

1. 400 if unread(A) is empty: "nothing unread for <registry_name>".
2. 409 if A's `last_state` is not `running`: "not running; resume it first".
3. Otherwise send the bundle as above. Nothing is posted to the channel.

Repeated retries are allowed; there is no server-side dedupe in v1.

### API

`GET /api/workspaces/{id}/unread?agent_id=` →

```json
{"agents": [
  {"agent_id": "ag_…", "registry_name": "claude-1", "read_mark": 140, "acked_above_mark": [143], "floor_id": 101,
   "count": 3,
   "messages": [{"id": 141, "sender": "ankit", "time": "…", "text": "first 200 chars…",
                 "routed_to": ["ag_…"]},
                {"id": 147, "sender": "codex-1", "time": "…", "text": "…", "routed_to": ["ag_…"]},
                {"id": 152, "sender": "ankit", "time": "…", "text": "…", "routed_to": ["ag_…"]}]}
]}
```

`routed_to` carries stable `agent_id`s (the routing side table's key), not
registry names. With `agent_id` the list holds that one agent; the shape is
the same either way. Here 143 was read out of order (in `acked_above_mark`),
so it is not listed; 141, 147 and 152 are routed to the agent, above the
mark, and not acked.
Without `agent_id`, one entry per agent in the workspace. The workspace list
route carries `unread_count` per agent from the same computation.

### CLI

- `/unread` — every agent in the current workspace: name, state, count, then
  each unread message as `#id  sender  text…`.
- `/unread <agent>` — one agent.
- `/retry <agent>` — send the bundle now. Prints the 400/409 text verbatim.
- The picker shows `unread N` next to each agent.

## 5. CLI

`cli.py` stays a thin client. All state and process control go through the
API in §2. The user-facing word is "session"; flags and commands use it.

### Startup: `python cli.py`

1. **Server** (D7) — interactive chat only. Probe `GET /api/status` on the port from `config.toml`.
   Down → `tmux new-session -d -s agentchattr-server "<sys.executable> run.py
   --port … --data-dir … --upload-dir … --mcp-http-port … --mcp-sse-port …"`
   (the values the CLI resolved from `config.toml` and any `AGENTCHATTR_*`
   overrides, passed explicitly because a session created on an existing
   tmux server inherits that server's environment, not the CLI's) from the
   repo root, with output appended to `data/logs/server.log`, then poll
   `/api/status` for up to 15 s. If a tmux session named `agentchattr-server`
   already exists while the port is down, do not create or kill anything:
   exit 1 naming that session and the manual command. Print one line:
   `Started server in tmux session agentchattr-server.` If tmux is missing or
   the server never answers, exit 1 with `Start it manually: python run.py`
   and the log location. `--url` given → never auto-start; the user chose a
   specific server. Shell commands never auto-start: they probe the chosen
   server and, if it is down, exit 1 with the manual-start hint — the same
   failure behaviour scripts see today.
2. **Picker**. `GET /api/workspaces`, newest `updated_at` first:

   ```
   Sessions
     1. billing refactor     claude-2 running · codex-1 exited (unread 3)     2h ago
     2. ws_9a1b2c            no agents                                        3d ago
     n. New session
     a. Show archived
   Choose:
   ```

   Empty list → straight to the new-session prompt. A workspace whose agent
   `cwd` no longer exists shows `⚠ cwd missing — /resume <agent> --cwd PATH`
   on that agent line, and the select-time resume question skips that agent.
   An agent whose `last_launch.kind` is `fresh` and whose state is `starting`
   shows `fresh` in place of its state. Two workspaces with the same name
   show the first four hex characters of their ids after the name.
   `--session <archived name>` prints that the session is archived and asks
   `Unarchive it? [y/N]`; no → exit 1. `sessions --archived` lists archived
   sessions in addition to active ones.
3. **New session**. Prompt for a name; blank → the id. `POST /api/workspaces`,
   then enter chat in its channel.
4. **Existing session**. Print one status line per agent. If any agent is
   `exited`, ask once: `Resume N stopped agents? [Y/n]`. Yes → `POST
   …/resume` for each; one that cannot be resumed (null id, missing cwd,
   name in use) prints its 409 text and is skipped, the rest proceed. No →
   chat only; `/resume` by hand. Then enter chat in its channel.
   `--session <id|name>` skips the picker and still asks; `--no-resume`
   skips the question (answer no) for scripts. `--channel` keeps today's
   behaviour — plain channel chat with no workspace — so scripts and the
   browser-created channels still work.

### In-chat commands (workspace only)

| Command | Behaviour |
|---|---|
| `/spawn <provider> [--agent-name NAME] [--cwd PATH] [--history-mode none\|literal\|summary]` | Missing `--cwd` → prompt, default = last cwd used in this workspace, else the CLI's cwd. Missing `--history-mode` → prompt; until slice 3 ships only `none`/`literal` are offered with `literal` as the default (the size-based `summary` default applies once slice 3 exists); an explicit `summary` is refused with the server's "not available" message. `--agent-name` sets a custom `registry_name`; default `<provider>-<n>`. (`--name` keeps its existing meaning everywhere: the human sender; `--history` stays the numeric history limit.) `POST …/agents`; prints `registry_name`, state. While `history_state` is `pending` prints `catching up…` (`summarizing…` once slice 3 exists and the mode is `summary`); on `done` prints so; on `failed` (slice 3 only — nothing in slice 1–2 writes `failed`) prints the reason and prompts `[r]etry / [l]iteral / [n]one`. Progress arrives via the WebSocket `workspace` event; the 2 s poll runs only while an agent is `starting` or `history_state` is `pending`, or while the WebSocket is down. |
| `/resume <agent> [--fresh] [--agent-name NAME] [--cwd PATH]` | `POST …/resume`. `--agent-name` takes a new registry name when the old one is held; `--cwd` re-points a moved project. On `native_session_id` null without `--fresh`: prints the refusal and the exact `--fresh` command. |
| `/stop <agent>` | `POST …/stop`. |
| `/attach <agent>` | Uses the agent's `tmux_session` field from the server (`agentchattr-<agent_id>`; derive it only if the field is absent). Outside tmux: `tmux attach -t <session>` in the foreground, run off the event loop so the receiver keeps draining; messages arriving during attach are buffered (bounded) and printed after detach (Ctrl+B D). Inside tmux (`$TMUX` set): `tmux switch-client -t <session>` — nested attach is refused by tmux — and the CLI prints how to switch back (`tmux switch-client -l`). If the tmux session is gone, prints `not running` and the resume hint. |
| `/agents` | Extended: for each workspace agent — `registry_name`, provider, state, cwd, unread count, native id present or not. Non-workspace agents listed below as today. |
| `/unread [agent]`, `/retry <agent>` | §4. |
| `/history` (no arguments) | Unchanged from today: prints recent channel history. |
| `/history <agent> <none\|literal\|summary>` | `POST …/history`. Resolves a failed summary or widens `none`. |
| `/rename <name>` | `PATCH /api/workspaces/{id}`. |
| `/archive` | Confirm `y/N`. Checkpoint, stop agents, archive, return to the picker. |
| `/sessions` | Checkpoint, then back to the picker without quitting. |
| `/quit` | Checkpoint (§2), then leave the CLI. Server and agents keep running (D8). |

`<agent>` accepts `registry_name` or `agent_id`; a bare provider name is
accepted when exactly one agent of that provider is in the workspace.

### Shell commands (non-interactive)

Same conventions as the existing `send`/`read`/`channels`/`status`: `--json`,
`--timeout`, non-zero exit on failure, errors on stderr.

```
cli.py sessions [--archived] [--json]
cli.py new <name> [--json]
cli.py spawn <provider> --session <id|name> --cwd PATH [--agent-name NAME] [--history-mode MODE] [--json]
cli.py resume <agent> --session <id|name> [--fresh] [--agent-name NAME] [--cwd PATH] [--json]
cli.py stop <agent> --session <id|name> [--json]
cli.py attach <agent> --session <id|name>
cli.py unread --session <id|name> [--agent X] [--json]
cli.py retry <agent> --session <id|name> [--json]
cli.py history <agent> --session <id|name> --mode MODE [--json]
cli.py archive <id|name> [--yes]
```

`send` and `read` gain `--session <id|name>` as an alternative to `--channel`;
it resolves to the workspace channel. `--session` and `--channel` together is
an error.

Name resolution for `--session`: exact id, then exact name, then unique name
prefix. Ambiguous → error listing the matches with ids.

### Windows

`chat`, `sessions`, `new`, `unread`, `retry`, `send`, `read` work. `spawn`,
`resume`, `attach` and server auto-start exit 1 with
`Requires tmux (Linux/macOS). See wrapper_windows.py for manual launch.` (D12).

## 6. Provider adapters and native session ids

`native_session_id` is what makes "resume" a real resume (requirement 8).
Everything provider-specific lives behind one adapter interface so a user can
add a vendor without touching core code. Verified against the installed
versions on 2026-09-12; the implementation plan re-verifies before coding.

### Package layout

```
providers/
  __init__.py     get_adapter(provider_name, agent_cfg) → ProviderAdapter
  base.py         ProviderAdapter protocol + NullAdapter
  generic.py      config-driven adapter, no Python needed for simple vendors
  claude.py       built-in
  codex.py        built-in
```

### The interface (`providers/base.py`)

```python
class ProviderAdapter(Protocol):
    name: str
    supports_resume: bool

    def allocate_session_id(self) -> str | None: ...
        # Return an id to pass on first launch (Claude: uuid4). None = provider assigns.
    def new_session_args(self, session_id: str | None) -> list[str]: ...
        # Extra CLI args for a fresh launch. Positionals first, then flags.
    def resume_args(self, session_id: str, cwd: Path) -> list[str]: ...
        # Args that resume the given conversation from cwd.
    def launch_env(self, launch: LaunchContext) -> dict[str, str]: ...
        # Extra environment for the provider process (codex: the originator override).
    def discover_session_id(self, launch: LaunchContext,
                            timeout: float) -> str | None: ...
        # For providers that assign ids: find it after launch. None on timeout.
    can_locate_transcripts: bool
        # True when locate_transcript is meaningful for this provider.
    def locate_transcript(self, session_id: str, cwd: Path) -> Path | None: ...
        # Path of the provider's own transcript, or None. Used by the resume pre-check
        # only when can_locate_transcripts is True.
    def summarizer_command(self, model: str | None, prompt_path: Path,
                           output_path: Path, workdir: Path) -> list[str] | None: ...
        # Headless summarise command for §3, or None if this provider cannot.
```

`LaunchContext` is a frozen dataclass: `agent_id`, `kind`
(`spawn | resume | fresh`), `launch_nonce` (uuid4 per launch), `cwd`,
`launched_at`, `provider_pid` (filled in by the wrapper's first `ready`
heartbeat, may be `None`). The launcher persists it as
`last_launch` on the agent entry and in the identity shadow, so discovery
can be re-run by checkpoint or reconcile after a server restart with the
same context the process was started with.

Every method except `name`, `supports_resume` and `can_locate_transcripts`
has a default in a base class
(`allocate_session_id → None`, `launch_env → {}`, `discover_session_id → None`,
`locate_transcript → None`, `summarizer_command → None`,
`new_session_args → []`). `resume_args` has no default; an adapter that does
not implement it has `supports_resume = False`.

`NullAdapter` is the base with nothing overridden: spawn works, resume is
refused unless `--fresh`.

### Resolution order (`providers.get_adapter`)

1. `adapter = "module.path:ClassName"` in the agent's `[agents.<name>]` table.
   Imported with `importlib`; the class is instantiated with `agent_cfg`.
   `config.local.toml` can add such an agent, so a user-written adapter needs
   only a Python file on `PYTHONPATH` (or in a top-level `adapters/` directory,
   which is added to `sys.path` and listed in `.gitignore` like
   `config.local.toml`). Loading code named in config is accepted because the
   same config already names the executable to run.
2. Built-in by provider name: `claude`, `codex`.
3. `generic` if the agent table has any of the generic keys below (phase 2).
4. `NullAdapter`.

### Generic adapter (`providers/generic.py`) — phase 2, not built in v1

v1 ships `base.py`, `claude.py`, `codex.py` and `get_adapter` with the
`adapter = "module:Class"` hook. The generic adapter is the intended next
step and its shape is fixed here so v1 does not paint it out. For vendors
whose CLI takes a flag for the id and a flag to resume, no code will be
needed:

```toml
[agents.acme]
command = "acme"
cwd = ".."
session_id_args = ["--session", "{session_id}"]   # optional; enables allocate_session_id
resume_args     = ["--resume", "{session_id}"]    # required for resume
transcript_glob = "~/.acme/sessions/**/{session_id}*.json"   # optional; enables pre-check
summarizer_args = ["-p", "--model", "{model}", "--no-tools"] # optional; prompt on stdin
```

`{session_id}`, `{cwd}` and `{model}` are the only placeholders. If
`session_id_args` is absent the id is provider-assigned and discovery is not
attempted (`native_session_id` stays `null`; resume refused unless `--fresh`).

### Claude (`providers/claude.py`)

- `allocate_session_id` → `uuid4()`; `new_session_args` → `["--session-id", id]`.
  The id is known before the process starts and is stored immediately.
- `resume_args` → `["--resume", id]`, launched from `cwd`.
- `locate_transcript` → glob `~/.claude/projects/*/<id>.jsonl`. The directory
  encoding of `cwd` is not relied upon.
- `summarizer_command` → `claude -p --model <m> --tools "" --no-session-persistence --strict-mcp-config`, prompt on stdin, default model `haiku`.
- `can_locate_transcripts = True`. Ten seconds after `running`, the launcher
  calls `locate_transcript`; found → `native_verified: true`, else the picker
  shows `id unverified`. Resume always runs the pre-check below, so an
  unverified id that never materialised is refused there, not attempted.

### Codex (`providers/codex.py`, verified at 0.154.0)

- `allocate_session_id` → `None`. Codex assigns the id.
- `discover_session_id` → rollout files land at
  `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`; line 1 is
  `{"type": "session_meta", "payload": {"id", "cwd", "timestamp", …}}`. The
  header carries `payload.originator`, which codex takes from the
  environment variable `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`. Spike
  (2026-09-12, codex 0.154.0): `codex exec` launched with the override wrote
  `"originator": "agentchattr:spike:abc123"` into `session_meta`, alongside
  the launch `cwd`. Note `codex exec` refuses a directory that is not a git
  repository unless `--skip-git-repo-check` is passed; interactive `codex`
  prompts instead. Correlation is launch-specific:
  1. **Launch** with `launch_env` returning
     `CODEX_INTERNAL_ORIGINATOR_OVERRIDE=agentchattr:<agent_id>:<launch_nonce>`,
     both taken from the `LaunchContext` (the wrapper already builds an
     `env(1)` prefix for the tmux command, so the value reaches the provider
     process itself). The same context is persisted as `last_launch`, so a
     checkpoint or reconcile after a server restart searches for the same
     originator string.
  2. **Candidates** = files with `payload.originator` equal to that exact
     string. `cwd` and `timestamp` are recorded but not used to match; an
     unrelated codex in the same directory, or a late file from an earlier
     timed-out launch, carries a different originator and is ignored.
  3. **Exactly one candidate** → store it. **Zero** after 60 s of 2 s polls
     → `null`, `history_note: "codex session id not found"`. **More than one**
     (should be impossible) → `null`, `history_note: "codex session id
     ambiguous"`. Never "newest wins".
  If the spike shows the override does not reach `session_meta`, the
  fallback is process correlation: the wrapper reports the provider pid;
  candidates are rollout files that pid holds open (`/proc/<pid>/fd` on
  Linux, `lsof -p` on macOS). If neither works, the adapter returns `null`
  and codex is spawn-only until it does — cwd-and-time matching is not an
  acceptable fallback.
  The picker shows `id unknown` for any null case; resume is refused unless
  `--fresh`.
- `resume_args` → `["resume", "-C", str(cwd), id]`. A positional subcommand —
  the launch builder takes `[positionals…] [flags…]`, not flags only.
- `can_locate_transcripts = True`; `locate_transcript` → glob
  `~/.codex/sessions/**/rollout-*-<id>.jsonl`.
- `summarizer_command` → `None` in v1 (§3: no proven tool-free mode). If the
  spike finds one, it becomes `codex exec <isolation flags> -m <m> -o
  <output_path> "<prompt>"` and this line is updated.

### Other bundled providers (`gemini`/`agy`, `kimi`, `qwen`, `kilo`, `codebuddy`, `copilot`)

Resolve to `NullAdapter` until someone writes an adapter or adds generic keys.
`/spawn` prints `resume not supported for <provider>; add an adapter or
resume_args to enable it`.

### Where adapters are used

- §2 spawn step 4 and resume step 3 (`allocate_session_id`,
  `new_session_args`, `resume_args`, `discover_session_id`).
- §2 resume pre-check. Two adapter capabilities, kept distinct:
  `supports_resume` — the adapter can build `resume_args`; without it resume
  is refused unless `--fresh`. `can_locate_transcripts` — the adapter can see
  the provider's transcript on disk; when `True`, `locate_transcript(id, cwd)`
  must return a path before resume launches, else 409 with the pattern
  searched and the `--fresh` hint, so a moved or re-created project directory
  fails loudly instead of starting a fresh conversation labelled as a resume;
  when `False` (a custom adapter for a remote or opaque provider), resume
  launches and the provider's own error is surfaced if the id is bad. Claude
  and codex are `True` for both.
- §3 summariser: `auto` picks the first adapter, claude before codex then
  config order, whose `summarizer_command` is not `None` and whose `command`
  is on `PATH`. In v1 that is claude only.

### Conformance test

`tests/test_provider_adapters.py` runs one parametrised suite over every
adapter `get_adapter` can return, using a temporary home directory: argument
lists contain no unformatted `{placeholder}`, `resume_args` raises or is
absent exactly when `supports_resume` is false, `locate_transcript` returns
`None` on an empty tree and the right path on a seeded one. A user-written
adapter can run the same suite against itself.

### What is never stored

Provider transcripts are never copied, parsed beyond line 1, or deleted.
Archive (D9) leaves them untouched.

## 7. Error handling

Rules: validate before side effects; every failure is visible to the user in
the CLI and recorded on the agent entry; nothing is silently re-labelled.

| Situation | Behaviour |
|---|---|
| Spawn/resume validation fails (provider unknown, cwd missing, command or tmux not on `PATH`, archived workspace) | 400 before registration or any file write. CLI prints the error verbatim. |
| Wrapper `Popen` fails | **Spawn:** deregister the identity, delete the identity file, remove the agent entry; 500 with the exception text. **Resume / fresh:** deregister the identity only; the entry stays with `last_state: exited`, `last_error` = the exception text; `native_session_id`, `previous_native_ids`, `floor_id`, `read_mark`, `acked_above_mark`, `history_*` and the identity shadow are untouched. A failed resume never loses what it was resuming. |
| Wrapper starts but no `ready` heartbeat within 60 s (spawn, resume or fresh) | The launcher kills the tmux session, terminates the wrapper (SIGTERM, SIGKILL after 5 s) and deregisters the identity itself (§2 step 7) — it does not wait for the registry's heartbeat timeout, which a wrapper sending `ready: false` forever would never trigger. Then `last_state: exited`, `last_error` = last 20 lines of `data/logs/wrapper-<agent_id>.log`. CLI prints `failed to start; see <log path>`. Saved state is untouched, as above. |
| Agent exits later (crash, `/exit`, provider error) | Existing deregister hook → `exited`. `purge_identity` drops the registry cursor only; `floor_id`, `read_mark` and `native_session_id` are on the record and untouched. Nothing restarts it (`--no-restart`). |
| Resume: transcript missing, `native_session_id` null, cwd gone, agent already running | 409 with the specific reason and the exact next command: `--fresh`, `--cwd PATH` (re-point; validated and persisted per the Resume steps), `--name NAME`, or nothing. |
| Resume cannot get its previous `registry_name` (held by a live instance) | 409 `name <old> in use; stop that agent or resume with --name <new>`. With `--name`, floor and `read_mark` are unchanged (keyed by `agent_id`); only the registry cursor starts fresh. |
| Summariser missing, timeout, non-zero exit, empty output | §3: no fallback. `history_state: failed`, `history_note: <reason>`, `system` message in channel, CLI prompt `[r]etry / [l]iteral / [n]one`. Floor unchanged. |
| Server restarts while a summary is pending | On reconcile, `pending` becomes `failed` with note "server restarted during summary". The user resolves it with `/history` or the prompt on next CLI start. |
| `workspaces.json` unreadable | Renamed to `workspaces.json.corrupt-<timestamp>`, store starts empty, server logs at ERROR, `GET /api/workspaces` carries `"warning"` and the picker prints it. Managed agents are still recognised through their identity shadows (§1 fail-closed rule), so their reads stay restricted; spawn and resume are refused until the corrupt file is dealt with. |
| Agent entry has no `floor_id` (record damaged) | Reads for that agent in that channel fail closed with the `/history` hint (§1). Never floor 0 by default. |
| Codex id discovery: zero candidates after 60 s, or more than one | `native_session_id` stays null, `history_note` says not found or ambiguous, picker shows `id unknown`, resume refused unless `--fresh`. Candidates are matched by the launch-specific originator only (§6). |
| `/attach` on an agent with no tmux session | `not running` plus the resume hint. Never creates a session. |
| Server auto-start: tmux missing or no `/api/status` within 15 s | Exit 1 with the manual command and, if it was started, the tmux session name to inspect. |
| Server already up on the configured port but reporting a different `data_dir` (`/api/status` gains `data_dir`) | Warning line, then continue. The user may intend it. |
| Identity file | Written with mode `0600`; it holds the bearer token and the policy shadow. Deleted on archive; on stop it is kept until resume or archive so the shadow survives an outage. |
| Two CLIs on one workspace | Fine. The server is the only writer; each CLI sees the other's changes through the WebSocket `workspace` event or on next poll. |

## 8. Testing

Same tooling as the repository: `unittest`, real server on temporary ports
and data paths for integration, no real provider CLIs ever launched. Tests
that need tmux skip with a clear reason when it is absent, the way the
Windows-only test skips today.

### Unit

- `test_workspace_store.py` — create/rename/archive/unarchive, sort order,
  atomic save (temp + rename), corrupt-file recovery, `rename_agent`.
- `test_visibility.py` — the predicate on `chat_read` (first, cursor,
  explicit `since_id` including 0 and −1 with oldest-first paging and
  `has_more`), `chat_resync`, `chat_summary(read)`, `__all__` reads;
  message id 0 visible under `literal`; audience filtering; floor and
  `read_mark` survive deregister, rename and resume under a new name;
  missing `floor_id` fails closed and `/history` restores reads;
  `/api/messages` with an agent bearer token is filtered by the predicate;
  `/api/export` returns 403 to an agent bearer token.
- `test_unread.py` — recipients recorded in the workspace routing table
  from `_handle_new_message` for WebSocket, MCP and `/api/send` messages; a `@claude-2` mention
  while claude-2 is stopped is recorded for it; a family `@claude` mention
  reaches stopped claude members; `@all` while claude-2 is stopped is
  **not** recorded for it; a no-mention message under
  `routing.default = "none"` has no recipients at all, and under `"all"`
  is recorded for running members only; a renamed recipient still matches by
  `agent_id`; `chat_send` after messages 141–149 leaves them unread;
  the bundle prompt for N = 1 and N > 1 (marker only when N > 1) and the
  over-50 truncation; resume delivers the bundle once and only after a
  `ready` heartbeat; `retry` 400/409 paths.
- `test_provider_adapters.py` — the conformance suite from §6 over claude,
  codex and `NullAdapter`, plus codex `discover_session_id` against a seeded
  fake `~/.codex/sessions` tree — one candidate with the launch originator,
  none, two concurrent launches into one cwd each finding only its own
  file, an unrelated rollout in the same cwd with a different originator
  ignored, a late file from an earlier launch ignored, and two files with
  the same originator yielding the ambiguous result — and claude
  `locate_transcript` against a fake `~/.claude/projects`.
- `test_summarizer.py` — provider selection, input capping, reuse of a fresh
  stored summary, and a PATH shim script standing in for `claude`/`codex`
  that returns text, returns nothing, exits non-zero, or hangs past the
  timeout; asserts the posted message and its `audience`, that other
  agents' `chat_read` never returns it, the trigger, and the `failed` state
  plus the `/history` resolutions.
- `test_workspace_launcher.py` — validation order (no side effects on 400),
  argument assembly for spawn and resume per adapter, `running` only on a
  `ready` heartbeat and `exited` after 60 s without one, reconcile on start,
  pending-summary handling on restart, identity file mode, stable tmux name
  across a rename.
- `test_wrapper_flags.py` — `--cwd`, `--identity-file`, `--no-attach`
  parsing and their effect on the MCP config path and registration call,
  using the existing wrapper test patterns.

### Integration (`tests/test_workspace_integration.py`)

Real server on temporary ports with a test-only `[agents.fake]` whose
`command` is a stub script that registers heartbeats, writes a fake
transcript, and idles in tmux. Covers: create workspace → spawn with each
history mode → `running` → expected queue entry → stop → resume with
pre-check → archive → unarchive; plus the shell commands with `--json`; plus
the picker driven by simulated input; plus server auto-start into a tmux
session with a temporary data dir (killed on cleanup). Requires tmux.

### Manual checklist (kept with the implementation plan)

Spawn real `claude` and `codex` from the CLI into a scratch project; confirm
`native_session_id` is captured for both; kill each; resume each and confirm
the provider shows the earlier conversation; confirm `/unread` shows a
mention sent while dead and `/retry` delivers it.

## 9. Build order

Three slices, each shippable and testable on its own, preceded by one
spike. The implementation plan may be one document with three parts or
three documents.

0. **Spike** — done 2026-09-12; answers recorded in §2 step 7, §2 resume
   step 2, §3 provider bullets and §6 codex: originator override confirmed;
   pane output ~2 s (trust prompt caveat); claude flags tool-free (with a
   confabulation caveat for the prompt); codex not tool-free, stays
   summary-less; claude resumes across directories.
1. **Server core** — `providers/` (base, claude, codex, `get_adapter`),
   floors in `mcp_bridge.py`, `routed_to` at the three routing sites,
   `workspace_store.py`, `workspace_launcher.py` (spawn, resume, stop,
   reconcile), wrapper flags, routes for workspaces, agents, unread, retry.
   History modes `none` and `literal` only.
2. **CLI** — server auto-start, picker, in-chat and shell commands, attach.
3. **Summary mode** — `workspace_summarizer.py`, audience filtering,
   `POST …/history`, the failed-state prompt, `history_state` reporting in
   the CLI.

Slice 1 alone satisfies requirements 3, 4, 5, 7 and 8 from the handover;
slice 2 adds 1, 2 and the user-visible half of 6; slice 3 completes 6.
