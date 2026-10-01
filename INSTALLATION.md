# Install yapp

yapp is a terminal UI and shell client for AI coding agents. It connects to a
local yapp server and manages chat sessions and terminal agents through that
server.

For a shorter walkthrough, see the [website setup guide](https://yapp.riggedcode.com/docs/).
This checklist is intended for both people and AI agents helping with installation.
yapp installs as a Python package straight from GitHub and provides two
equivalent commands, `yapp` and `goon`. A source checkout is only needed for
development. The older release ZIP builder does not include all CLI modules.

## 1. Check the system

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
sudo apt-get install python3 python3-venv python3-pip git tmux curl
```

Confirm Python is still at least 3.11 afterward; older distributions may need
a newer Python interpreter. On macOS, with Homebrew already installed:

```sh
brew install python git tmux
```

## 2. Install the package (recommended)

Run the installer. It needs only Python 3.11+ with `venv` support and `curl`:

```sh
curl -fsSL https://yapp.riggedcode.com/install.sh | sh
yapp --help
goon --help
```

If the website is unreachable, use the same script from GitHub:
`curl -fsSL https://raw.githubusercontent.com/Ankitkkkk/yapp/main/install.sh | sh`.

The installer creates a private virtual environment in `~/.local/share/yapp/venv`,
installs the latest published release (or `main` if there is no release yet), and links `yapp` and `goon` into `~/.local/bin`. It
never overwrites an existing command with those names, and it prints the exact
uninstall command. yapp then keeps itself up to date (see the README's
*Staying up to date*); running the installer again also updates it. If it
reports that `~/.local/bin` is not on `PATH`, add it as shown and open a new
terminal.

`yapp` and `goon` are the same application; use whichever name you prefer.

### Alternative: pipx

[pipx](https://pipx.pypa.io/) also works, but it must be installed first
(`sudo apt install pipx` on Ubuntu/Debian, `brew install pipx` on macOS, then
`pipx ensurepath`), and installing from a `git+` URL needs `git`:

```sh
pipx install git+https://github.com/Ankitkkkk/yapp.git
```

A pipx install updates itself like the installer's does (see the README's
*Staying up to date*). To update by hand, run `yapp update`; `pipx upgrade yapp`
follows the `main` branch it was installed from rather than published releases.
If the shell offers to correct `pipx` to `pip`, decline: plain
`pip` on a system Python fails with `externally-managed-environment` (PEP 668).
Never use `--break-system-packages` for yapp.

An installed package keeps its runtime state outside the package, so upgrades
do not touch it: `~/.local/share/yapp/data` and `~/.local/share/yapp/uploads`
(`$XDG_DATA_HOME/yapp` when set; `%LOCALAPPDATA%\yapp` on Windows). Set
`YAPP_DATA_DIR` or `YAPP_UPLOAD_DIR` to use other locations.

Use **absolute project paths** in Add agent or `--cwd`.

## 3. Install from a source checkout (development)

Clone the repository, or reuse an existing checkout. If a checkout already
exists, inspect it and preserve local changes instead of cloning over it or
resetting it. Install it in editable mode so code changes apply immediately:

```sh
git clone https://github.com/Ankitkkkk/yapp.git
cd yapp
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip check
.venv/bin/yapp --help
```

If `.venv` already exists, check its Python version and reuse it when compatible.
Do not delete an existing environment without understanding who uses it.
Preserve the `mcp<2.0` dependency constraint: the server uses
`mcp.server.fastmcp`.

A source checkout keeps runtime state in `data/` and `uploads/` inside the
checkout. `python yapp.py` and `python cli.py` also work from the repository
root with the same arguments.

## 4. Start and verify the local server

On Linux/macOS, opening the TUI from an interactive terminal starts a missing
local server automatically in the `yapp-server` tmux session (unless an
explicit `--url` was supplied):

```sh
yapp
```

Default listeners:

| Purpose | Address |
| --- | --- |
| Web UI and local API | `http://127.0.0.1:8300` |
| MCP streamable HTTP | `http://127.0.0.1:8200/mcp` |
| MCP SSE | `http://127.0.0.1:8201/sse` |

With the server running, check it from another terminal:

```sh
yapp status --json
yapp sessions --json
```

These checks read state; they do not send messages or start provider agents,
and shell commands such as `status` do not auto-start the server. An empty
session list on a new installation is normal. From a source checkout without
activating `.venv`, use `.venv/bin/yapp status --json`.

To run the server manually in a source checkout (for example on native Windows
or to watch its output), run `.venv/bin/python run.py` from the repository root.
Starting only `uvicorn app:app` omits the shared server/MCP initialization.

If ports are occupied, identify the existing service first. Do not kill another
installation. Custom ports and data locations use `config.toml` or matching
`YAPP_*` environment overrides; `config.local.toml` adds new agent definitions
and may override `[updates]` settings; it does not override server settings or
existing agent definitions. See `config_loader.py` for the exact
supported overrides. Never expose the server publicly as an installation shortcut.

## 5. Add the user's first agent

Install and authenticate only the provider CLI the user chooses, following that
provider's own instructions. Check it is on the server's `PATH` and can run in
an ordinary terminal. Provider accounts and credentials are separate from
yapp; login should use the provider's normal interactive flow.

In yapp:

1. Create or select a session. Creating one in the TUI asks for its name,
   then an orchestrator provider and absolute working directory. Submitting
   starts that provider; install and authenticate it first. A shell
   `yapp new NAME` creates a session without an orchestrator unless
   `--orchestrator-provider PROVIDER --cwd /absolute/project` is supplied.
2. Use **Add agent**, or **F3 → Add agent**, to add a worker.
3. Select the provider, enter an agent name and an absolute project directory.
4. Set optional provider flags and history mode, then submit.
5. Send a message mentioning the agent name, such as `@reviewer`. In the TUI,
   press **i** to compose, then **Escape** followed by **Enter** to send.

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
When already inside tmux, use `tmux switch-client -l` to return to the previous
session.
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
| Enter in INSERT mode | Accept a completion if selected; otherwise insert a newline |
| Escape, then Enter | Return to NORMAL mode and send |
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
| `yapp` / `goon`: command not found | Add `~/.local/bin` to `PATH` (or run `pipx ensurepath`) and open a new terminal, or use `.venv/bin/yapp` from a checkout. |
| `error: externally-managed-environment` | Plain `pip` ran against system Python (often a `pipx` → `pip` shell correction). Use the installer or install pipx first; never `--break-system-packages`. |
| Installer: could not create a virtual environment | Install venv support (`sudo apt install python3-venv`, or `python3.X-venv` for your version) and rerun. |
| Missing Python modules | Rerun the installer (or `pipx reinstall yapp`, or `pip install -e .` in a checkout) and run `pip check`. |
| Interactive chat requires a terminal | Launch in a real terminal; use `read`, `status`, or other shell commands for scripts. |
| Full-screen unavailable | Check terminal size and TERM; try `yapp --plain` for the scrolling client. |
| Could not connect | Start `run.py`, inspect its errors, and verify the URL/ports. Do not share token-bearing logs. |
| tmux not found / unsupported lifecycle | Install tmux on Linux/macOS, or run inside WSL on Windows. |
| Agent missing or stuck | Check the provider executable/login, agent cwd, Activity (F5), and Attach (F6) for approval prompts. |
| UI looks unchanged after an update | Exit and reopen yapp. Reload the server for backend changes after accounting for active work. |

Runtime state lives in `~/.local/share/yapp/` for package installs, or `data/`
and `uploads/` in a source checkout; preserve these when updating. Also preserve `config.local.toml`, provider login files, and existing
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
