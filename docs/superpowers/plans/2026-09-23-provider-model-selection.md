# Provider-native Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Delegate only when authorized; preserve the shared working tree.

**Goal:** Select Claude Code and Codex models from their installed provider's metadata, persist the choice for workers/orchestrators, and support trusted custom discovery/application hooks.

**Architecture:** Extend existing ProviderAdapters with model discovery/application, backed by one bounded server service and short-lived metadata helpers. Persist an optional model separately from raw CLI flags and frozen profiles. Shared API/CLI fields feed a reusable asynchronous TUI picker.

**Tech Stack:** Python 3.11+, unittest, FastAPI, prompt-toolkit, subprocess/JSON protocols, optional Claude Agent SDK dependency. Keep `mcp<2.0`.

**Spec:** [Provider-native model selection design](../specs/2026-09-23-provider-model-selection-design.md).

**Status:** Proposed; no implementation performed. Resume-editable models are the recommended assumption unless the user changes it. No live model switching.

## Global constraints

- Python 3.11 or newer; keep `mcp<2.0`.
- Discovery: 10-second total deadline, 1 MiB combined output, 1000 models, 20 pages, two concurrent helper processes.
- Cache: memory only, 60 seconds, maximum 64 contexts; Refresh bypasses agentchattr's cache.
- No inference requests, automatic agent launches, project hooks or MCP startup during discovery.
- Preserve config.local.toml's add-only agent-entry semantics.
- Model values are provider-native opaque strings; no hardcoded model catalog or cross-provider alias map.
- Provider settings means no agentchattr typed override; saved raw flags/native resume behavior remain effective.
- A model is configurable on creation or while stopped, separately from the immutable role/personality profile.
- No live user history, provider configuration mutation or paid sessions in automated validation.
- Full-suite/PTY tests use temporary ports/data/uploads, removed inherited TMUX and a private TMUX_TMPDIR with registered cleanup.
- This checkout already contains substantial unrelated uncommitted work. Do not commit, reset, stash or stage it wholesale. Begin implementation from an agreed isolated snapshot/worktree containing that baseline; stage only this feature when integration is requested.

## Task 1: Establish metadata-only provider compatibility

**Files:** Create `docs/provider-model-compatibility.md`, `tests/fixtures/provider_models/codex_model_list.json`, `tests/fixtures/provider_models/claude_initialize.json`. Create `tests/test_provider_model_protocol.py` during Task 3, using these fixtures. Candidate optional dependency: `requirements-models.txt`.

**Produces:** Documented supported CLI/auth/config contexts, minimal sanitized protocol fixtures, and a go/no-go decision for the Claude discovery transport. No default provider model list is derived from fixtures.

- [ ] Read both builtin adapters, wrapper command assembly, launch/resume/orchestrator flow, and the spec's source links.
- [ ] In a temporary directory, capture installed `claude --version`, `claude --help`, `codex --version`, `codex resume --help` and version-generated schema. Do not launch interactive providers:

```python
with tempfile.TemporaryDirectory(prefix="model-contract-") as directory:
    subprocess.run([codex_path, "app-server", "generate-json-schema",
                    "--out", directory], check=True, timeout=10)
```

- [ ] Inspect the official Claude SDK dependency metadata in a disposable virtual environment. Choose and record a tested SDK constraint compatible with Python 3.11 and this repo's `mcp<2.0`; use its explicit `cli_path` so discovery never silently switches to the bundled binary. Do not add a Node runtime requirement.
- [ ] Build a disposable initialization-only probe against controlled settings/auth stubs: Codex sends initialize/initialized/model-list only; Claude connects without a prompt and obtains server initialization info. Configure sentinel project hooks and MCP commands that create a marker if started; require no marker, no inference messages, no persistent conversation and no remaining process. Preserve an allowlist/model-policy fixture to ensure suppressing executable hooks did not discard model context.
- [ ] Where scoped credentials are available, verify metadata-only enumeration separately and record exact CLI/SDK/auth mode, settings inputs, result source and any provider-owned cache writes. Do not use inference to test entitlement or silently switch the authentication backend.
- [ ] Record the compatibility gate: do not claim Claude support complete until its metadata initialization, policy/auth parity and shutdown pass. If an SDK/CLI combination cannot safely expose the list, return an explicit unsupported/dependency/configuration state; do not substitute an HTTP API catalog, binary scraping or hardcoded models. Resolve transport limitations before the full feature release.

**Review gate:** Readable evidence distinguishes docs/source support from runtime verification and includes native-only Claude installations. Initial research already checked CLI versions/help and Codex schema; no real catalog probe has yet run.

## Task 2: Add contracts, bounded processes and catalog service

**Files:** Create `provider_models.py`, `providers/model_process.py`, `tests/_model_helpers.py`, `tests/test_provider_models.py`, `tests/test_model_process.py`; modify `providers/base.py`, `tests/test_provider_adapters.py`, and lifecycle wiring in `run.py`/`server_lifecycle.py` where helpers are owned.

**Interfaces:** Define all immutable values, errors and three service methods from the spec. Define `UNCHANGED_MODEL = object()` for internal resume omission. Add safe-default adapter methods `discover_models`, `model_options(..., launch_kind=...)`, and `compose_launch_args`.

- [ ] Add a test helper `model_context(root, provider='codex', provider_args=()) -> ModelContext` whose command/cwd/settings/env all point to temporary fixtures. Never read the developer's auth directory.
- [ ] Write failing conformance and invalid-data tests, including the backwards-compatible base behavior:

```python
class ModelDefaultsTests(unittest.TestCase):
    def test_native_and_legacy_arguments_keep_order_without_model(self):
        adapter = ProviderAdapter()
        self.assertEqual(adapter.compose_launch_args(
            ['resume', 'saved-id'], (), ('--verbose',)),
            ['resume', 'saved-id', '--verbose'])

    def test_unsupported_discovery_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            context = model_context(Path(directory))
            with self.assertRaises(ModelDiscoveryError) as caught:
                ProviderAdapter().discover_models(context, timeout=10)
            self.assertEqual(caught.exception.code, 'unsupported')
```

- [ ] Add inert child programs that emit malformed JSON, terminal control text, >1 MiB output, duplicate values, a hanging response and an owned grandchild. Test deadlines, combined-output limits, reaping and cancellation. Use safe platform-specific process-tree cleanup; skip/report unsupported platform integration rather than silently testing only the parent.
- [ ] Implement immutable contracts and strict validation. Reject empty/newline/NUL/control-bearing selection values, values over 512 characters, bad output shape, duplicate conflicting entries and environment collisions; sanitize labels/descriptions separately. Keep opaque values intact, including aliases, slashes, brackets and non-ASCII text.
- [ ] Implement an injected-clock/process-backed service: bounded LRU cache, identical-request coalescing, two-child admission, Refresh, safe typed failures and an explicit close/drain. No subprocess I/O while holding cache or launcher lifecycle locks.
- [ ] Test context changes (binary identity, cwd, profile/config/auth metadata, model-affecting environment, hook revision), stale labels, discovery failure recovery, waiter cancellation and shutdown admission. A cancelled caller must not kill shared work still owned by another caller; shutdown must stop all owned probes.
- [ ] Run focused unittest discovery for `test_provider_models.py`, `test_model_process.py` and `test_provider_adapters.py`; confirm legacy adapters still work without overriding any model method.

**Deliverable:** Model discovery cannot hang the server or leak owned processes; unsupported providers remain spawn-compatible.

## Task 3: Implement Claude/Codex discovery and model argument rules

**Files:** Modify `providers/claude.py`, `providers/codex.py`; create `providers/claude_model_probe.py`, `tests/test_provider_model_protocol.py`, `tests/test_provider_model_arguments.py`, `requirements-models.txt` if Task 1 passed. Keep wire code separate if it makes builtin files unwieldy.

**Consumes:** Task 1 compatibility results/fixtures; Task 2 transport/contracts. **Produces:** Both builtin `discover_models()` and `model_options()` implementations.

- [ ] Write protocol transcript tests that fail if any user prompt/thread/turn/tool message is sent. Include a Codex row whose `id != model`, two pages with distinct cursors, repeating cursors, unknown optional fields, hidden rows and a provider error. For Claude include alias `value` differing from `resolvedModel`, missing models and initialization failure.
- [ ] Add adapter-owned selector grammar tests: `--model value`, `--model=value`, verified short forms, Codex `-c model=...`/`--config=model=...`, inline Claude model settings, terminators and malformed values. Verify unrelated fallback/reasoning flags survive unchanged. Explicit selection plus any primary-selector spelling must fail before launch.

```python
class ModelArgumentTests(unittest.TestCase):
    def test_codex_explicit_model_does_not_override_raw_model_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            context = model_context(Path(directory), provider_args=('--model=raw',))
            with self.assertRaises(ModelSelectionError) as caught:
                CodexAdapter().model_options(context, 'catalog-value',
                                             launch_kind='resume')
            self.assertEqual(caught.exception.code, 'conflict')

    def test_claude_preserves_alias_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            context = model_context(Path(directory), provider='claude')
            options = ClaudeAdapter().model_options(context, 'fixture-alias',
                                                    launch_kind='spawn')
            self.assertEqual(options.args, ('--model', 'fixture-alias'))
```

- [ ] Implement Codex stdio handshake/pagination and normalize `model` values. Preserve account/config/profile/backend context according to Task 1's verified grammar; unsupported backend/context combinations return a typed configuration/unsupported result.
- [ ] Implement Claude short-lived Python SDK helper against `context.command`, using the proven settings to disable executable startup work while keeping model policy. Connect with no prompt, obtain initialization models, disconnect in finally, and emit only normalized catalog JSON. Optional dependency absence returns installation guidance without installing anything automatically.
- [ ] Implement provider-owned primary-model conflict checks and `('--model', value)` application. Do not resolve an alias to another model, send a canary inference, alter provider config, or consume stdin as a prompt during discovery.
- [ ] Run the protocol/argument/adapter suites. Repeat the controlled compatibility probe only if Task 3 changes the proven wire/config options. Record concrete backend/auth/platform limits in `docs/provider-model-compatibility.md`.

**Deliverable:** Fresh catalog values can appear without application updates, and builtin model flags have verified semantics for the exercised CLI versions.

## Task 4: Compose trusted custom discovery/application hooks

**Files:** Create `providers/model_hooks.py`, `examples/model_hooks.py`, `tests/test_model_hooks.py`; modify `providers/__init__.py`, `config.local.toml.example`, `README.md`.

**Interface:** `ModelHookAdapter` delegates existing lifecycle/identity/transcript methods to a wrapped adapter, overrides configured model operations only, and passes `launch_kind` to apply. Exact JSON schema and config example live in the spec.

- [ ] Write fixture hooks for discovery-only override, apply-only override, both operations, custom provider without builtin application, invalid JSON/schema, nonzero exit, excessive output and timeout.
- [ ] Test argv paths containing spaces, quotes and shell metacharacters, literal opaque model values, reserved environment key rejection, and builtin resume/correlation methods remaining unchanged. Verify HTTP payload data cannot select a hook executable or alter its command.
- [ ] Implement hook composition through the same bounded transport. Discovery returns model data; apply returns `args` and `env`. Validate fragments and reserved environment collisions before launch. Keep hook configuration local-only and preserve config.local add-only merging.
- [ ] Ship a complete example hook that reads one JSON request, checks `schema_version`/operation, discovers through a configurable inert fixture command in its example, and emits versioned JSON. The apply example returns an argv array; no shell command construction:

```python
if request['operation'] == 'apply':
    response = {'schema_version': 1,
                'args': ['--model', request['model']], 'env': {}}
    print(json.dumps(response))
```

- [ ] Document trusted-code execution, installation paths, inherited provider authentication, time/output bounds, no-inference discovery requirement, conflict checks, custom `compose_launch_args`, and how a new provider alias preserves MCP injection. A `command='codex'` alias currently inherits Codex MCP defaults; arbitrary wrapper executables may require explicit MCP settings.
- [ ] Run `test_model_hooks.py`, `test_provider_adapters.py`, `test_wrapper_mcp_config.py` and `test_config_overrides.py`.

**Deliverable:** A user can add a newly discovered model and apply its native arguments without editing core application code.

## Task 5: Persist selections and integrate all launch paths

**Files:** Modify `workspace_store.py`, `workspace_launcher.py`, `provider_args.py`, `wrapper.py`; test `tests/test_workspace_store.py`, `tests/test_workspace_launcher.py`, `tests/test_wrapper_flags.py`, `tests/test_agent_profiles_lifecycle.py`, `tests/test_orchestration_integration.py`; create `tests/test_workspace_models.py`.

**Interfaces:** Spawn accepts `model=None`; resume/configure-orchestrator accept `model=UNCHANGED_MODEL`. Request-only `catalog_key`/`keep_saved` are preflight inputs, not persisted fields. `_wrapper_command` receives complete composed provider argv. Internal wrapper `--provider-command PATH` selects the prepared executable for managed launches.

- [ ] Extend temporary launcher/store fixtures to include a catalog service double and inert model-aware provider. Add failing tests for omitted/null/string persistence, old records, new/fresh/native resume, unchanged frozen profiles and failed-launch rollback.
- [ ] Assert complete final argv, not merely stored selection: exactly one typed model pair, native session/resume arguments retained, original raw flag order, wrapper MCP args and prompt hooks intact, correlation env retained, and `provider_args` not appended twice.
- [ ] Implement preflight outside lifecycle locks; reacquire and revalidate workspace/agent state, arguments/cwd, command identity and admission state before registration or persistence. Cover concurrent resumes, agent already running, archive, Stop all, server restart and config/binary changes during lookup.
- [ ] Save `model`, migrate absent fields to null on read, and include `requested_model` in the launch snapshot. Generate model args at launch time; never inject them into saved `provider_args`. Roll back the model together with old args/identity on synchronous failure.
- [ ] Pin and check the managed provider executable through the wrapper option; leave direct legacy wrapper invocation lookup unchanged. Keep path quoting and environment handoff correct in Unix and Windows branches.
- [ ] Route orchestrator creation, stopped resume, fresh fallback and recovery through the same composition. Recovery reuses saved models without catalog access. Reject requested model/args/cwd changes to a running orchestrator; leave idempotent unchanged enable valid.
- [ ] Test that invalid selection/conflict causes zero registry/store/wrapper mutation and that unavailable discovery does not block an explicit Keep saved or null/default legacy launch. A newly chosen value still needs a matching valid catalog.
- [ ] Run focused lifecycle/store/wrapper/orchestration regressions under temporary fixtures. These shared changes also require the final isolated full suite.

**Deliverable:** Selections survive restart/resume and reach the correct provider invocation without corrupting identity or prior launch state.

## Task 6: Expose model capabilities through human API and CLI

**Files:** Modify `app.py`, `cli.py`, `cli_workspaces.py`, `cli_workspace_chat.py`, `cli_view_contracts.py`; create `tests/test_provider_models_api.py`, `tests/test_cli_models.py`; extend `tests/test_workspace_api.py`, `tests/test_cli_workspace_commands.py`.

**Interfaces:** `WorkspaceAPI.models(provider, cwd, provider_args, refresh=False)` calls `POST /api/providers/{provider}/models`. Capabilities include `provider_models: 1`. Existing spawn/resume/orchestrator endpoints accept `model`, `catalog_key`, `keep_saved` with exact omission semantics.

- [ ] Write API tests for strict body keys/types, known providers/absolute cwd, human session token/origin checks, agent bearer rejection, safe error messages, and no child created before auth/validation. Missing CLI is distinct from empty catalog.
- [ ] Add capability-gated WorkspaceAPI and controller plumbing. No API accepts hook paths/commands/environment. Execute blocking discovery off the event loop; service owns cancellation/drain. Return sanitized labels/source/time/context key without raw env or credentials.
- [ ] Preflight orchestrator model selection before saving a new workspace. Verify failure after launch reservation follows existing recoverable saved-workspace response and does not encourage duplicate creation.
- [ ] For shell/API model strings without a receipt, let server `prepare_selection` use a fresh context-matching cache or perform bounded discovery before mutation; reject unknown values. Expired same-context receipts refresh/revalidate; mismatching-context receipts fail. Omitted resume model and null/default need no lookup. Shell explicit same-saved selection sends `keep_saved=true` after fetching the saved agent; the server verifies equality. Add tests for cold cache, valid/expired/mismatched receipts, missing model, discovery failure and zero mutation on failure.
- [ ] Extend parser and dispatch with `models PROVIDER --cwd PATH [--provider-flags TEXT] [--refresh]`, spawn `--model`, resume mutually exclusive `--model` / `--provider-default-model`, and new `--orchestrator-model`. Preserve omission with `argparse.SUPPRESS`:

```python
choice = resume_parser.add_mutually_exclusive_group()
choice.add_argument('--model', default=argparse.SUPPRESS)
choice.add_argument('--provider-default-model', action='store_true',
                    default=argparse.SUPPRESS)
if getattr(args, 'provider_default_model', False):
    body['model'] = None
elif hasattr(args, 'model'):
    body['model'] = args.model
```

- [ ] Cover JSON/text catalog output, discovery `--provider-flags` quote parsing/forwarding and cache invalidation, native values with punctuation, invalid flags, explicit same saved model/Keep saved, and old-server preflight. Explicit model selection against an old server errors before mutation; ordinary legacy commands still work.
- [ ] Pass configured model through shared agent views; do not label it as verified serving state or entitlements. Keep agent identity/token and frozen-profile validation unchanged.
- [ ] Run API/client/parser/controller tests with mocked providers and isolated HTTP instances.

**Deliverable:** Terminal surfaces share one consistent model contract; scripts can enumerate and select models without hand-authoring provider flags.

## Task 7: Add the reusable TUI model picker

**Files:** Create `cli_tui_models.py`, `tests/test_cli_tui_models.py`; modify `cli_tui_dialogs.py`, `cli_tui_view.py`, `cli_view_contracts.py`, `tests/_tui_harness.py`, `tests/test_cli_tui_agent_fields.py`, `tests/test_cli_tui_workflows.py`.

**Interface:** `choose_model(ui, *, provider, cwd, provider_args, saved_model, current_model) -> ModelChoice | None`; define immutable `ModelChoice(model, catalog_key=None, keep_saved=False)`. None means cancel; `ModelChoice(model=None)` means Use provider settings. Do not overload these two meanings.

- [ ] Write asynchronous harness tests at 80x18 and a wide layout. Cover loading without blocking other UI work, searchable choices, source/age, keyboard/mouse selection, Refresh, escaped/long labels, empty/error state and missing saved model.
- [ ] Implement one compact searchable picker using current Catppuccin controls. Begin discovery on entering the picker or Refresh, not on keystrokes/rendering. Use an incrementing request generation and full form context key to ignore late responses.
- [ ] Keep provider settings available during a lookup failure. Keep saved is a distinct deliberate selection. Disable only submission that needs a pending new choice; Cancel/default remain responsive. Preserve earlier form values and errors until corrected.
- [ ] Integrate add/resume/new-session orchestrator/enable-orchestrator flows, with no extra model page against old servers. Show exact chosen value in the existing final form/context before launch.
- [ ] Add races: provider/cwd/flag edit after starting lookup, session switch, closed form, Refresh superseding prior request, stop/resume conflict, failure then successful retry clearing the error. Assert late results trigger zero mutations.
- [ ] Assert composer draft/cursor/Vim mode and originating form focus survive picker cancel/success; mouse selection and subsequent `i` input still work. Update stale step-count/focus expectations only where the new intentional flow changes them.
- [ ] Add compact configured-model display/details and run focused TUI suites. Capture real cell layouts rather than treating stripped ANSI as a rendered screen.

**Deliverable:** Model choice works in narrow terminals without introducing another input-mode regression.

## Task 8: End-to-end verification, documentation and review

**Files:** Create `tests/test_provider_models_integration.py`; extend inert-CLI cases in `tests/test_cli_tui_integration.py`; update `README.md`, `docs/provider-model-compatibility.md`, hook example documentation and this plan's completion checklist.

- [ ] Use real isolated run.py, API/controller/TUI and inert Claude/Codex PATH shims whose model lists change between requests. Verify the new option appears after Refresh and its exact value reaches final spawn/resume argv. Include one orchestrator and a hook-backed provider.
- [ ] Assert independent identity/native session IDs remain unchanged where expected; resume omission preserves, explicit null removes only the typed override, failed preflight produces no saved mutation, and supervisor recovery never invents a replacement model.
- [ ] Test cancellation/quit/restart drain while a fake catalog child is hung, final child reaping, and concurrent discovery limits. Verify no project-hook/MCP/inference sentinel was invoked.
- [ ] Run the repository's full unittest suite using the exact outer isolated runner from AGENTS.md; keep mcp<2.0 and install optional CLI/model dependencies only in the test environment. Capture results/skips. Do not run unisolated discovery against the default tmux socket.
- [ ] Perform 80x18 and wide PTY checks with inert commands, mouse/Esc/`i`/Vim mode, then document actual kitty/terminal color QA only if performed. Mock/PTY tests do not establish paid model availability or Windows end-to-end behavior.
- [ ] Have a reviewer inspect protocol/context authenticity, hook trust boundaries, argument composition, saved-state rollback, running-orchestrator conflicts and async focus/races. Coordinate in AGENTS_CONVO.md and reply to each finding.
- [ ] Run `git diff --check`, review only feature changes, and record verified CLI/SDK versions and unsupported contexts. Do not claim all providers/accounts work from fixtures alone.
- [ ] Report implementation status and remaining limitations. Commit/push only under the user's applicable integration instruction, staging just this feature rather than unrelated pending changes.

## Coverage map

| Requirement | Tasks |
| --- | --- |
| Catalogs from installed Claude/Codex, no model-turn discovery | 1–3, 8 |
| Correct flags, matching executable/context, raw-flag conflicts | 2–3, 5, 8 |
| User discovery/application hooks and unchanged config precedence | 4, 8 |
| Saved model, resume/fresh, frozen-profile separation, rollback | 5–6, 8 |
| Resident orchestrator and running-edit rejection | 5–7, 8 |
| Responsive compact TUI, default/errors/cache/late-result behavior | 2, 6–8 |
| Old records, old servers, raw legacy arguments | 2, 5–7 |
| Process bounds, authentication, resource release | 2, 4, 6, 8 |
