# Install yapp

yapp is a terminal UI and shell client for AI coding agents. It connects to a
local yapp server and manages chat sessions and terminal agents through that
server.

This guide is intended for both people and AI agents helping with installation.
Installation currently uses a **source checkout**. There is no published
`pip install yapp` package provided by this repository. The older release ZIP
builder does not include all CLI modules; use the checkout containing this guide.

## 1. Check the system and choose a checkout

For the full experience, use Linux or macOS with:

- Python **3.11 or newer**, including `venv` and `pip`.
- Git and **tmux**. tmux is required for server auto-start and terminal-agent
  spawn, resume, stop, and attach.
- An interactive terminal at least **80 columns × 18 rows**. A wider terminal
  shows the session sidebar. Set a valid `TERM` through your terminal application.
- At least one supported provider CLI if the user wants to add agents. Installing
  yapp alone does not install or authenticate those CLIs.

On Windows, use **WSL2 with a Linux distribution** for terminal-agent management
and follow the Linux instructions inside WSL. Native Windows can use chat with a
manually started server, but this CLI's tmux lifecycle actions are unavailable.
Native Windows full-screen behavior is not validated end to end.

Run these checks before installing anything:

```sh
python3 --version
git --version
tmux -V
```

On Ubuntu/Debian, missing system dependencies can be installed with:

```sh
sudo apt-get update
sudo apt-get install python3 python3-venv python3-pip git tmux
```

Confirm Python is still at least 3.11 afterward; older distributions may need
a newer Python interpreter. On macOS, with Homebrew already installed:

```sh
brew install python git tmux
```

Use the repository URL and revision supplied by the user or distributor of this
guide. Do not assume an upstream default branch contains these CLI changes.
If a checkout already exists, inspect it and preserve local changes instead of
cloning over it or resetting it. Locate the directory containing **all** of:

```text
yapp.py
cli.py
run.py
config.toml
requirements-cli.txt
```

That directory is the repository root for every command below. The enclosing
`agent-collab` directory in some development layouts is not this root.

## 2. Install Python dependencies

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-cli.txt
.venv/bin/python -m pip check
.venv/bin/python yapp.py --help
```

If `.venv` already exists, check its Python version and reuse it when compatible.
Do not delete an existing environment without understanding who uses it.
`requirements-cli.txt` includes the server requirements. Preserve its `mcp<2.0`
constraint: the server uses `mcp.server.fastmcp`.

No activation is required when using `.venv/bin/python` explicitly. The existing
`python cli.py` entry point remains compatible with the same arguments.

## 3. Make a shell command (Linux, macOS, WSL)

The application is called **yapp**; you choose the shell command's name during
installation (for example `yapp` or `goon`). Run the installer from the
repository root:

```sh
./install.sh                # asks for the command name (default: yapp)
./install.sh --name goon    # or pass any name non-interactively
```

It creates `.venv` if needed, installs `requirements-cli.txt`, and writes a
launcher to `~/.local/bin/<name>` (override with `YAPP_BIN_DIR`) that uses the
checkout's virtual environment, so users do not need to activate it. Help text
shows the chosen name. The installer only replaces launchers it created itself;
if an unrelated command already exists at that path, it stops without
overwriting it. Run it again with another name to add a second command.

Ensure `~/.local/bin` is on `PATH`:

```sh
export PATH="$HOME/.local/bin:$PATH"
yapp --help
```

Add that export once to the user's shell startup file if needed (`~/.bashrc` for
interactive Bash, or `~/.zshrc` for Zsh). Do not duplicate an existing entry.

The launcher starts in the checkout directory to keep default data paths stable.
Use **absolute project paths** in Add agent or `--cwd`; relative paths through
this launcher resolve from the checkout. If the checkout moves, recreate the
launcher with its new path after inspecting the old launcher.

## 4. Start and verify the local server

For the first installation, start the server in a separate terminal, from the
repository root:

```sh
.venv/bin/python run.py
```

Default listeners:

| Purpose | Address |
| --- | --- |
| Web UI and local API | `http://127.0.0.1:8300` |
| MCP streamable HTTP | `http://127.0.0.1:8200/mcp` |
| MCP SSE | `http://127.0.0.1:8201/sse` |

Keep the server running and check it from another terminal:

```sh
yapp status --json
yapp sessions --json
```

Without the optional launcher, run the same commands as
`.venv/bin/python yapp.py status --json` from the repository root.
These checks read state; they do not send messages or start provider agents.
An empty session list on a new installation is normal.

Then open the TUI from an interactive terminal:

```sh
yapp
```

On Linux/macOS, interactive startup can automatically start a missing local
server in tmux when no explicit `--url` was supplied. Shell commands such as
`status` do not auto-start it. An explicit `--url` requires an already running
server. Use `run.py` for manual startup; starting only `uvicorn app:app` omits
the shared server/MCP initialization.

If ports are occupied, identify the existing service first. Do not kill another
installation. Custom ports and data locations use `config.toml` or matching
`YAPP_*` environment overrides; `config.local.toml` adds agent definitions
only and does not override server settings. See `config_loader.py` for the exact
supported overrides. Never expose the server publicly as an installation shortcut.

## 5. Add the user's first agent

Install and authenticate only the provider CLI the user chooses, following that
provider's own instructions. Check it is on the server's `PATH` and can run in
an ordinary terminal. Provider accounts and credentials are separate from
yapp; login should use the provider's normal interactive flow.

In yapp:

1. Create or select a session.
2. Use **Add agent**, or **F3 → Add agent**.
3. Select the provider, enter an agent name and an absolute project directory.
4. Set optional provider flags and history mode, then submit.
5. Send a message mentioning that agent by its session name.

`--agent-name` names an agent; `--name` names the human sender. `@all` or `@both`
addresses the running agents in the selected session. `@everyone` is not an alias.
New and resumed agents receive their session identity in their startup prompt.

Provider flags are passed as arguments, not evaluated as shell commands. For
shell usage, quoting a value with leading dashes is simplest with `=`:

```sh
yapp spawn codex --session work --agent-name reviewer \
  --cwd /absolute/path/to/project --provider-flags='--model MODEL_NAME'
```

Replace the example provider, session, directory, and model with the user's
choices. Do not run this example as a smoke test: it starts a real provider.

When an agent needs approval, use **F6**, choose it, and resolve the prompt in its
terminal. **Ctrl+B, then D** detaches from tmux while leaving the agent running.
Ctrl+C can stop the provider; use Resume to restore it. Resume keeps the saved
directory and provider flags by default. Waiting indicators aid discovery, but
the provider's own approval prompt remains authoritative.

## Everyday controls

| Key | Action |
| --- | --- |
| F1 | Help |
| F2 | Sessions or channels |
| F3 | Choose an agent, including Add agent |
| F4 | Commands, including Loop guard (1–50) |
| F5 | Activity / back to chat |
| F6 | Choose an agent to attach |
| F7 | Release mouse capture for terminal text selection; press again to return |
| Escape | Close the current overlay while preserving the message draft |
| Alt+Enter | Insert a newline |
| Ctrl+Q | Quit the TUI |

For copying, press F7, drag over text, and use the terminal's Copy shortcut
(Ctrl+Shift+C in GNOME Terminal). In tmux or terminals that still capture the
mouse, try Shift-drag or tmux copy mode. Copy behavior depends on the terminal.

The loop guard limit is shared across sessions; counters are channel-specific.
Saving a higher limit does not unpause an already paused conversation. Send
`/continue` in that conversation after adjusting it.

## Troubleshooting and agent handoff

| Symptom | Check or action |
| --- | --- |
| `<name>`: command not found | Check the launcher and `~/.local/bin` in PATH, or use `.venv/bin/python yapp.py` directly. |
| Missing Python modules | Install `requirements-cli.txt` using the exact virtual environment that launches yapp; run `pip check`. |
| Interactive chat requires a terminal | Launch in a real terminal; use `read`, `status`, or other shell commands for scripts. |
| Full-screen unavailable | Check terminal size and TERM; try `yapp --plain` for the scrolling client. |
| Could not connect | Start `run.py`, inspect its errors, and verify the URL/ports. Do not share token-bearing logs. |
| tmux not found / unsupported lifecycle | Install tmux on Linux/macOS, or run inside WSL on Windows. |
| Agent missing or stuck | Check the provider executable/login, agent cwd, Activity (F5), and Attach (F6) for approval prompts. |
| UI looks unchanged after an update | Exit and reopen yapp. Reload the server for backend changes after accounting for active work. |

Runtime state normally lives in `data/` and `uploads/`; preserve these when
updating. Also preserve `config.local.toml`, provider login files, and existing
project-specific configuration. Generated `.codex/hooks.json` files contain
installation-specific paths and should not be copied from another person's
checkout; managed launches configure supported waiting hooks locally.

An assisting agent should finish by reporting:

- OS, Python interpreter/version, and checkout path/revision used.
- The exact launch command, and whether PATH was changed.
- Whether `pip check`, CLI help, and read-only server checks passed.
- Whether the TUI was visually checked; do not infer that from `--help`.
- Which provider, if any, was installed/authenticated or started.
- Any remaining manual login or terminal-specific steps.

For updates, inspect local changes before pulling, install updated requirements,
and restart affected processes deliberately. Do not delete user data, reset the
checkout, send test chat messages, or terminate unrelated tmux sessions as part
of installation verification.
