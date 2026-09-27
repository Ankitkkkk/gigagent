# Provider-native model selection

Date: 2026-09-23
Status: Proposed design; planning only. No model-selection implementation or live provider discovery was run.

## Intent and scope

Python 3.11 or newer; keep `mcp<2.0`.

Choose the model for a terminal agent from metadata reported by that provider's installed CLI. Support Claude Code and Codex first, including the resident session orchestrator. Let users supply local discovery and application hooks for other providers or custom deployments. The provider integration owns both discovery and correct CLI argument construction.

Recommended behavior, pending the user's optional preference: choose at creation, remember the selection, and allow changing it when a stopped agent is resumed, including a fresh conversation. No live switching in this first version. Model selection is separate from the immutable role/personality profile.

A catalog establishes what the provider reports, not a guarantee of account entitlement, quota, network availability or which model eventually serves a request. Display a **configured model**, never a supposedly verified running model. Provider-default behavior may depend on configuration and resumed conversation state.

## Current code and evidence

- `providers/base.py` already defines `ProviderAdapter`, lifecycle arguments and environment hooks. `providers/__init__.py` loads custom `module:Class` adapters.
- `workspace_launcher.py` currently combines native spawn/resume arguments with saved `provider_args`; `wrapper.py` prepends MCP arguments and launches the configured command. The final command, not only the form payload, needs coverage.
- `workspace_store.py` persists agent arguments and frozen profiles; ordinary resume rolls fields back after a synchronous launch failure. Orchestrator recovery also goes through this launcher.
- `cli_tui_dialogs.py` has separate provider/cwd, profile, resume and orchestrator forms. Add one reusable asynchronous model picker rather than growing every dialog vertically.
- `config.local.toml` adds new agent entries only. It cannot override existing builtin entries. This design preserves that rule.
- Read-only local research found Claude Code `2.1.280` and Codex CLI `0.156.1`; both advertise `--model`. Codex resume also accepts it. Generating Codex's JSON schema in `/tmp` confirmed the installed model-list request/response shapes. These observations are compatibility fixtures, not a claimed minimum supported version.

Codex provides paginated `model/list` after app-server initialization. Map `model` to the launch value and `displayName` to the label; `id` is a separate catalog identifier. Defaults and results depend on context. Use the installed CLI's schema and tolerate additional optional fields. [Official app-server documentation](https://learn.chatgpt.com/docs/app-server#models).

Anthropic's published `@anthropic-ai/claude-agent-sdk` package `0.3.280`, inspected without installing or executing it, declares `supportedModels()` and an initialization response containing `models`. Each model has a `value`, label and optional `resolvedModel`; retain the selection value, including aliases, rather than substituting its resolved value. The Python SDK exposes initialization information through `get_server_info()` and accepts an explicit CLI path. These provide the implementation route, but initialization-only compatibility and settings parity still need a controlled probe. [SDK repository](https://github.com/anthropics/claude-agent-sdk-typescript), [Python reference](https://code.claude.com/docs/en/agent-sdk/python).

Claude settings can restrict choices and map aliases/deployment identifiers. Preserve those settings and environment inputs during discovery. A general Anthropic/OpenAI HTTP model list is not a substitute for the terminal agent's catalog. [Claude model configuration](https://code.claude.com/docs/en/model-config).

## User experience

1. **Add agent:** select provider, working directory and any advanced flags, then choose Model. The existing role/personality step stays intact. Selecting Model opens a searchable list with `Use provider settings`, model labels, exact values and Refresh.
2. **Resume:** preselect the saved model. Show `Keep saved: <value>` separately from `Use provider settings`. A changed selection applies to this next launch and subsequent resumes. Normal and fresh resume share this behavior.
3. **Orchestrator:** reuse the same picker during session creation and orchestrator enable/configure/resume. Orchestrator model configuration remains independent from worker models.
4. **Discovery:** show Loading, a Cancel action and a short source/version/age line. Keep the UI responsive. Changing provider, cwd or context-affecting advanced flags invalidates the result and selection receipt. Ignore late results after a dialog closes or changes context.
5. **Unavailable discovery:** retain the form and all fields. Offer Retry, Use provider settings, or Keep saved when applicable. Explain unsupported CLI, missing dependency, login/configuration failure and timeout separately. Never fill the list with guessed model names.
6. **Missing saved model:** retain it as `Saved value — not in current list`. Keeping that exact value requires an explicit human choice in the resume form; an absent catalog row does not prove the provider rejects it. Do not silently replace it.
7. **Appearance and input:** use the current Catppuccin styles and existing Vim/focus rules. A searchable picker owns typing while open. Esc returns to the originating form and restores focus. Support 80x18, keyboard and mouse; do not add per-model nested buttons.
8. **Agent details:** display `Configured model: <value>` or `Provider settings`, with a badge for a saved value whose current availability was not checked. Keep the compact agent pane readable through truncation; put full values in details/activity.

`Use provider settings` means yapp supplies no typed override. Existing advanced flags, provider configuration, environment and native resume behavior still apply. The form should say when advanced flags already select a model. Clearing an yapp override does not promise to reset a model recorded in the provider's native conversation.

## Data and interfaces

Introduce `provider_models.py` for immutable values, validation, discovery service and cache; `providers/model_process.py` for bounded subprocess/protocol transport; and `providers/model_hooks.py` for trusted external hook adaptation. Keep provider wire decoding in the Claude/Codex modules or small adjacent modules.

```python
@dataclass(frozen=True)
class ModelOption:
    value: str                 # exact provider launch token
    label: str
    description: str = ""
    is_default: bool = False
    resolved_value: str | None = None  # information only

@dataclass(frozen=True)
class ModelContext:
    provider: str             # configured agent entry, including aliases
    command: Path             # exact resolved executable
    cwd: Path
    provider_args: tuple[str, ...]
    environment: Mapping[str, str]     # private; never serialized to clients
    cli_version: str
    fingerprint: str          # opaque context key, includes hook revision

@dataclass(frozen=True)
class ModelCatalog:
    options: tuple[ModelOption, ...]
    source: str               # codex-app-server, claude-initialize, custom-hook
    context_key: str
    fetched_at: str
    stale: bool = False

@dataclass(frozen=True)
class ModelLaunchOptions:
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
```

Adapter additions, with backwards-compatible defaults:

```python
def discover_models(self, context: ModelContext, *, timeout: float) -> ModelCatalog:
    raise ModelDiscoveryError("unsupported", "Model discovery is unavailable")

def model_options(self, context: ModelContext, model: str,
                  *, launch_kind: str) -> ModelLaunchOptions:
    raise ModelSelectionError("unsupported", "Model selection is unavailable")

def compose_launch_args(self, native_args: list[str],
                        model_args: tuple[str, ...],
                        user_args: tuple[str, ...]) -> list[str]:
    return [*native_args, *model_args, *user_args]
```

`ModelDiscoveryError` and `ModelSelectionError` carry a stable `code` and safe human `message`. Supported discovery codes: `unsupported`, `missing_cli`, `dependency_missing`, `auth`, `configuration`, `timeout`, `invalid_response`, `busy`, `cancelled`. Selection adds `conflict`, `context_changed` and `unknown_model`. Do not expose subprocess output wholesale.

The service exposes:

```python
ModelCatalogService.discover(context, *, refresh=False) -> ModelCatalog
ModelCatalogService.prepare_selection(context, model, *, catalog_key=None,
                                      saved_model=None, keep_saved=False,
                                      launch_kind="spawn") -> ModelLaunchOptions
ModelCatalogService.close() -> None
```

Persist `model: str | None` on the saved agent, including the saved orchestrator agent; its agent record is the single source of truth rather than a duplicate model field on the workspace control record. Missing fields in old records load as `None`. Omitted model on resume preserves the saved value; JSON null clears yapp's override; a string replaces it. The request-only `keep_saved` can only authorize an unchanged saved value, never an arbitrary new one. Catalog values are bounded strings, not executable fragments. Labels/descriptions are sanitized terminal text and never used to build commands.

Reject attempts to change model, cwd or arguments on a running orchestrator with HTTP 409; its current configure path otherwise silently returns success without applying changes. Idempotent re-enable with unchanged settings remains valid.

Record `requested_model` in `last_launch` for diagnostics. Do not store credentials, hook environment, raw catalogs or catalog receipts in saved agents. A failed synchronous launch restores model and provider arguments together. A wrapper that starts then exits keeps the intended configuration and displays its real startup failure through the existing lifecycle.

## Discovery lifecycle and context

- Resolve the configured command using the same rules as launch. Use the same cwd and effective authentication/backend/settings environment after the wrapper's `strip_env` rules. Read model-affecting inputs without printing credentials.
- Builtin adapters interpret their own relevant config/profile/backend arguments; never forward arbitrary raw flags to a discovery process. Preserve model policy while disabling executable startup work. If a combination cannot be reproduced safely, report unsupported configuration and allow provider settings/Keep saved/custom hook.
- Cache in memory for 60 seconds, maximum 64 contexts; coalesce identical inflight requests. Key includes provider entry, real executable and file identity/version, cwd, relevant config/profile/auth-context revision and hook configuration revision. Context fingerprints are process-local opaque values, not raw tokens or plain hashes of secrets.
- Check known configuration/auth file metadata for invalidation, bypass on Refresh, and label age. Providers may cache their own results; refreshing yapp does not guarantee a remote refetch. No persistent credential-derived cache keys.
- A discovery has a 10-second total deadline, at most 1 MiB combined output, at most 1000 models, and at most 20 paginated responses. Cap global helper concurrency at two. Always close stdin, terminate owned process trees when needed, wait/reap, and release capacity on completion, cancellation, server shutdown or error. Never kill unrelated processes.
- No background polling, resident discovery process or per-agent catalog process. Refresh is user driven. No model turn, prompt, tool invocation, agent registration, tmux session, history write, project hook or MCP startup should be required. Provider-owned cache/auth maintenance may happen during initialization; do not promise a completely write-free native process.
- New selections require a valid matching catalog result. `prepare_selection` obtains that result itself when a shell/API caller supplies a new model without `catalog_key`: use a fresh matching cache entry or perform one bounded discovery before any mutation, then require the exact value to be present. Shell callers do not have to manage receipts. An expired receipt for unchanged context triggers bounded refresh and membership revalidation within that same request; an explicitly mismatching context returns `context_changed` without launching. A missing value, failed refresh or timeout preserves saved state and returns an actionable error. Never retry a mutation automatically.
- A known stale result can be displayed, but Refresh/validation must succeed before selecting a new value. Resume omission preserves the saved model without rediscovery. Explicit Keep saved (including a shell `--model` equal to the fetched saved value, sent with `keep_saved=true`) can also bypass discovery only for that unchanged value; it still runs launch/conflict validation. Clearing to null needs no catalog. Automated orchestrator recovery retains the exact saved value without adding a discovery dependency; real provider failures remain visible and never select a replacement model.

### Codex

Run the resolved executable in app-server stdio mode in the target cwd. Initialize the connection, then page through `model/list`, and close it. Never send thread/start, thread/resume or turn/start. Use model values, preserve default metadata and initially request picker-visible models. A saved hidden value remains visible as saved but unlisted. Normalizing unknown optional capability fields must not break listing.

Version-specific CLI settings/profile/backend handling must be verified against generated local schemas and help. Do not claim local OSS/third-party backends are covered by a ChatGPT catalog; if the adapter cannot discover that selected backend accurately, return unsupported configuration or delegate to a hook.

### Claude Code

Use the initialization metadata exposed by the Claude Agent SDK against the explicit resolved native CLI, without calling query with a prompt. Preferred transport is the official Python SDK in a short-lived helper, with a tested optional dependency file `requirements-models.txt`; do not silently use the SDK-bundled CLI. Read the `models` initialization field and require valid `value`/`displayName` entries. No UI scraping or extraction of model strings from the binary.

Before committing to this transport, verify it can retain effective account/model-policy context while suppressing tools, MCP, session persistence and executable startup hooks. Empty setting sources alone would lose policy and is not an acceptable shortcut. Missing SDK, missing metadata or an unverified CLI/auth mode is a clear capability failure, not an empty successful catalog. If the supported SDK transport cannot meet these conditions, leave Claude discovery explicitly unavailable in that context and revise the transport design before declaring Claude support complete. The first implementation milestone is this feasibility gate.

## Applying selections and avoiding conflicting flags

For Claude and Codex the typed model becomes `['--model', value]` within the adapter's launch argument composition. Cover initial launch, fresh and native resume. Selection uses the catalog's command value, never its display label, resolved alias target or catalog row ID.

- A null typed selection leaves legacy `provider_args` unchanged and adds no model flags.
- With a typed selection, reject raw arguments that also explicitly select the primary model. Detect the builtin CLI's separate/equal/short forms, Codex `-c/--config model=...` and any inline settings override that explicitly selects a model. Do not silently remove tokens or rely on ambiguous ordering.
- Provider config files/environment defaults remain in force; the explicit CLI flag follows the provider's documented precedence. Managed restrictions remain authoritative.
- Do not treat unrelated flags such as Claude `--fallback-model` as the primary selection. Such flags are still user controlled; actual serving fallback cannot be ruled out by yapp.
- Ambiguous argument syntax whose effects the adapter cannot establish gets a specific configuration error for typed selection; raw legacy launch remains possible with no typed selection. Respect `--` boundaries; never place a model option in a positional prompt.
- Compute model options once before registry/store mutation, outside launcher locks; reacquire the lifecycle lock and revalidate the saved agent, provider/cwd/args and command fingerprint before reserving a launch. Do not let slow discovery hold a workspace lock or start after Stop all/server shutdown admission closes.
- Pin the resolved executable for managed wrapper launch through an internal `--provider-command` option. Check its file identity/version against the prepared context before handing off. Direct legacy wrapper invocations retain normal command lookup. This prevents discovery from using one installation and launch from finding another on PATH.
- Refactor `_wrapper_command` to accept the fully composed provider argv without appending saved arguments a second time. Preserve existing MCP injection, native session identifiers, Codex correlation environment, prompt hooks, and Unix/Windows quoting.
- A hook may supply model-specific environment keys, but must not override agent identity, server connection/storage, prompt-event, PATH/interpreter or Codex launch-correlation keys. Reject collisions before launch. Do not allow hooks to rewrite application-managed environment.

## Custom discovery/application hooks

Keep `adapter = 'module:Class'` for full provider extensions. Additionally allow `model_hooks` within a locally trusted agent definition so users can replace just model discovery/application while retaining builtin resume/transcript support. Hooks run as direct argv arrays, `shell=False`, with versioned JSON on stdin/stdout. No string interpolation, `eval`, shell fragments, remote-configurable executable path or implicit package installation.

Example in `config.local.toml`, using a new entry because local overrides of builtins are not merged:

```toml
[agents.my_codex]
command = 'codex'
adapter = 'providers.codex:CodexAdapter'

[agents.my_codex.model_hooks]
discover = ['/absolute/path/python3', '/absolute/path/models.py', 'discover']
apply = ['/absolute/path/python3', '/absolute/path/models.py', 'apply']
```

To customize the original builtin entry, edit its `[agents.codex.model_hooks]` or `[agents.claude.model_hooks]` in `config.toml`. Do not extend config.local precedence as part of this feature. A `command = "codex"` alias inherits existing Codex MCP defaults through wrapper command detection; custom wrapper executables may require explicit MCP injection settings. Test both cases so a model hook does not silently drop MCP wiring.

Discovery input:

```json
{"schema_version":1,"operation":"discover","provider":"my_codex","command":"/path/codex","cwd":"/project","provider_args":[]}
```

Discovery output:

```json
{"schema_version":1,"models":[{"value":"team-model","label":"Team model","description":"Local deployment","is_default":true}]}
```

Apply input adds `model`, `launch_kind` and the saved/current raw `provider_args`. Apply output supplies only a model argument fragment and optional model environment:

```json
{"schema_version":1,"args":["--model","team-model"],"env":{}}
```

The adapter composes native arguments, model fragment and raw arguments in that order. Custom adapters may override `compose_launch_args` for CLIs with different positional rules. The hook receives no registration token or native session identity. Application-owned environment is stripped from its inherited environment; provider authentication is inherited as needed rather than duplicated into JSON. Hooks are trusted local executable code, not sandboxed plugins.

Discovery-only hooks can reuse builtin model application. Apply-only hooks can reuse builtin discovery. A provider with no native application implementation must configure apply as well. An empty hook result is an error for a non-null selection. Validate schema, strings, bounds, duplicate values, NUL/control characters, environment names and collisions. Hooks must reject their own conflicting raw model flags. New models are returned as data by a hook; no core source edit is required.

## API and CLI

- Advertise `provider_models: 1` through the existing capabilities endpoint.
- Human-authenticated `POST /api/providers/{provider}/models` accepts `{cwd, provider_args, refresh}` and returns normalized catalog metadata or a typed safe error. Reuse browser session-token/origin protection; agent bearer identities cannot launch discovery helpers. Validate provider/cwd before helper creation. Do not accept executable/hook configuration in the body.
- Existing spawn, resume and orchestrator payloads accept `model`; selection requests may include matching `catalog_key` and `keep_saved`. Resume omission/null/string semantics must survive CLI/controller serialization. Workspace agent views include the configured model.
- Preflight model validation for a new orchestrator before creating its workspace; failures after actual spawn follow existing saved-workspace error behavior without duplicating sessions.
- Add `models PROVIDER --cwd PATH [--provider-flags TEXT] [--refresh]` with text and JSON output, `spawn --model VALUE`, `resume --model VALUE | --provider-default-model`, and `new --orchestrator-model VALUE`. Omitted flags preserve compatibility. Explicit same-value resume can act as Keep saved. Use `argparse.SUPPRESS` or an explicit sentinel rather than treating omitted and null as equivalent.
- Plain interactive chat and the full-screen TUI use the same WorkspaceAPI/controller fields. Old servers show unavailable selection controls and reject an explicitly requested model locally; never silently discard it.

## Verification and delivery

Use fake provider processes and temporary settings/auth stubs for automated tests. Fail the tests on any inference request, unplanned hook/tool/MCP startup, shared tmux contact or changes to live data. Run final shared-lifecycle/API changes under the repository's outer isolated full-suite runner.

Acceptance covers: exact dynamic catalog values including a newly added fake model; Codex pagination and id/model mismatch; Claude alias/resolved-value distinction; no query/turn traffic; deadlines/output caps/cleanup/concurrency; cache context invalidation; custom hooks with spaces and non-ASCII values; shell metacharacters remaining literal; conflicts before mutations; null/omitted selection and rollback; native/fresh resume; resident orchestrator recovery; HTTP auth/origin boundaries; 80x18 loading, keyboard/mouse, Esc and late-result races; and exact final wrapper argv/environment on both platform paths with inert commands.

A real initialization-only compatibility check may be performed during implementation in a disposable environment with deliberately scoped provider credentials if available. Report that separately from mock tests. Do not claim catalog enumeration establishes paid inference, every account/backend, Windows terminal UX or native resume serving behavior.

Out of scope: hot model switching, automatic model upgrades, model-based orchestrator routing, reasoning-effort/service-tier controls, bulk changing worker models, a pricing catalog, API-backed wrapper model migration, or a new browser model-management UI.
