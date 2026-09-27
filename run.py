"""Entry point — starts MCP server (port 8200) + web UI (port 8300)."""

import argparse
import os
import asyncio
import secrets
import sys
import time
import logging
from pathlib import Path

# Ensure the project directory is on the import path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Start agentchattr (web UI + MCP server).",
        epilog="Flags override config.toml for this invocation. The same flags "
               "are also accepted by wrapper.py and wrapper_api.py so a launcher "
               "can isolate per-project instances by passing matching values to "
               "each process.",
    )
    parser.add_argument("--data-dir",      default=None, help="Override server.data_dir (path)")
    parser.add_argument("--port",          default=None, help="Override server.port (int)")
    parser.add_argument("--mcp-http-port", default=None, help="Override mcp.http_port (int)")
    parser.add_argument("--mcp-sse-port",  default=None, help="Override mcp.sse_port (int)")
    parser.add_argument("--upload-dir",    default=None, help="Override images.upload_dir (path)")
    parser.add_argument("--allow-network", action="store_true",
                        help="Allow binding to non-localhost hosts (with confirmation).")
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Preserve interpreter flags as well as the original application arguments.
    from server_lifecycle import ServerLifecycle, _RESTART_PARENT
    restart_argv = [sys.executable, *sys.orig_argv[1:]]

    # Parse flags for --help support; the actual env propagation happens via
    # the shared config_loader.apply_cli_overrides helper so run.py and the
    # wrappers use identical extraction logic.
    _parse_args()

    from config_loader import apply_cli_overrides, load_config
    apply_cli_overrides()

    config_path = ROOT / "config.toml"
    if not config_path.exists():
        print(f"Error: {config_path} not found")
        sys.exit(1)

    config = load_config(ROOT)

    host = config.get("server", {}).get("host", "127.0.0.1")
    restart_supported = os.name == 'posix' and host in ('127.0.0.1', 'localhost', '::1')
    lifecycle = ServerLifecycle(restart_argv, restart_supported=restart_supported,
        previous_instance_id=os.environ.pop(_RESTART_PARENT, None),
        reason='' if restart_supported else
        'Restart from the TUI requires a localhost server on Linux or macOS. Restart run.py manually.')

    # --- Security: generate a random session token (in-memory only) ---
    session_token = secrets.token_hex(32)

    # Configure the FastAPI app (creates shared store)
    from app import app, configure, set_event_loop, store as _store_ref
    configure(config, session_token=session_token, lifecycle=lifecycle)
    # Keep this instance's endpoints and storage on reload, including values
    # originally supplied by config.toml rather than command-line overrides.
    lifecycle.restart_environment = {
        'AGENTCHATTR_PORT': str(config.get('server', {}).get('port', 8300)),
        'AGENTCHATTR_MCP_HTTP_PORT': str(config.get('mcp', {}).get('http_port', 8200)),
        'AGENTCHATTR_MCP_SSE_PORT': str(config.get('mcp', {}).get('sse_port', 8201)),
        'AGENTCHATTR_DATA_DIR': config['server']['data_dir'],
        'AGENTCHATTR_UPLOAD_DIR': str(Path(config.get('images', {}).get('upload_dir', './uploads')).resolve()),
    }

    # Share stores with the MCP bridge
    from app import store, rules, summaries, jobs, room_settings, registry, router as app_router, agents as app_agents, session_engine, session_store
    import mcp_bridge
    mcp_bridge.store = store
    mcp_bridge.rules = rules
    mcp_bridge.summaries = summaries
    mcp_bridge.jobs = jobs
    mcp_bridge.room_settings = room_settings
    mcp_bridge.registry = registry
    mcp_bridge.config = config
    mcp_bridge.router = app_router
    mcp_bridge.agents = app_agents

    # Enable cursor and role persistence across restarts
    data_dir = ROOT / config.get("server", {}).get("data_dir", "./data")
    mcp_bridge._CURSORS_FILE = data_dir / "mcp_cursors.json"
    mcp_bridge._load_cursors()
    mcp_bridge._ROLES_FILE = data_dir / "roles.json"
    mcp_bridge._load_roles()

    # Terminal sessions: server-owned launcher (spec §2)
    import app as app_module
    from workspace_launcher import WorkspaceLauncher
    app_module.workspace_launcher = WorkspaceLauncher(
        store=app_module.workspace_store, messages=store, registry=registry, agents=app_agents,
        config=config, data_dir=data_dir, root=ROOT, background=lifecycle.start_worker)
    app_module.wire_workspace_hooks()
    app_module.initialize_orchestration()
    app_module.workspace_launcher.reconcile()
    for _ws in app_module.workspace_store.list(include_archived=True):
        app_module._ensure_channel(_ws["channel"])
    app_module._replay_unrouted()

    def _launcher_tick():
        while not lifecycle.stop_event.wait(5):
            try:
                app_module.workspace_launcher.tick()
                app_module.orchestration.tick()
                app_module._compact_routing_marks()
            except Exception:
                logging.getLogger(__name__).exception("launcher tick failed")

    lifecycle.start_worker(_launcher_tick)

    # Start MCP servers in background threads
    http_port = config.get("mcp", {}).get("http_port", 8200)
    sse_port = config.get("mcp", {}).get("sse_port", 8201)
    mcp_bridge.mcp_http.settings.port = http_port
    mcp_bridge.mcp_sse.settings.port = sse_port

    # Own all three uvicorn servers so a restart drains both MCP transports as
    # well as the browser/CLI listener. The FastMCP factories match its run APIs.
    import uvicorn
    for bridge, transport in ((mcp_bridge.mcp_http, 'http'), (mcp_bridge.mcp_sse, 'sse')):
        mcp_app = bridge.streamable_http_app() if transport == 'http' else bridge.sse_app()
        server = uvicorn.Server(uvicorn.Config(mcp_app, host=bridge.settings.host,
            port=bridge.settings.port, log_level=bridge.settings.log_level.lower(),
            timeout_graceful_shutdown=5))
        lifecycle.add_server(server)
        lifecycle.start_worker(server.run)
    time.sleep(0.5)
    logging.getLogger(__name__).info("MCP streamable-http on port %d, SSE on port %d", http_port, sse_port)

    # Mount static files
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import HTMLResponse

    static_dir = ROOT / "static"

    @app.get("/")
    async def index():
        # Read index.html fresh each request so changes take effect without restart.
        # Inject the session token into the HTML so the browser client can use it.
        # This is safe: same-origin policy prevents cross-origin pages from reading
        # the response body, so only the user's own browser tab gets the token.
        html = (static_dir / "index.html").read_text("utf-8")
        injected = html.replace(
            "</head>",
            f'<script>window.__SESSION_TOKEN__="{session_token}";</script>\n</head>',
        )
        return HTMLResponse(injected, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Capture the event loop for the store→WebSocket bridge
    @app.on_event("startup")
    async def on_startup():
        set_event_loop(asyncio.get_running_loop())
        # Resume any sessions that were active before restart
        if session_engine:
            session_engine.resume_active_sessions()

    # Run web server
    import uvicorn
    host = config.get("server", {}).get("host", "127.0.0.1")
    port = config.get("server", {}).get("port", 8300)

    # --- Security: warn if binding to a non-localhost address ---
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"\n  !! SECURITY WARNING — binding to {host} !!")
        print("  This exposes agentchattr to your local network.")
        print()
        print("  Risks:")
        print("  - No TLS: traffic (including session token) is plaintext")
        print("  - Anyone on your network can sniff the token and gain full access")
        print("  - With the token, anyone can @mention agents and trigger tool execution")
        print("  - If agents run with auto-approve, this means remote code execution")
        print()
        print("  Only use this on a trusted home network. Never on public/shared WiFi.")
        if "--allow-network" not in sys.argv:
            print("  Pass --allow-network to start anyway, or set host to 127.0.0.1.\n")
            sys.exit(1)
        else:
            print()
            try:
                confirm = input("  Type YES to accept these risks and start: ").strip()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm != "YES":
                print("  Aborted.\n")
                sys.exit(1)

    print(f"\n  agentchattr")
    print(f"  Web UI:  http://{host}:{port}")
    print(f"  MCP HTTP: http://{host}:{http_port}/mcp  (Claude, Codex)")
    print(f"  MCP SSE:  http://{host}:{sse_port}/sse   (Gemini)")
    print(f"  Data:    {data_dir}")
    print(f"  Agents auto-trigger on @mention")
    print(f"\n  Session token: {session_token}\n")

    web_server = uvicorn.Server(uvicorn.Config(app, host=host, port=port,
        log_level="info", timeout_graceful_shutdown=5))
    lifecycle.add_server(web_server)
    try:
        web_server.run()
    finally:
        lifecycle.begin_shutdown()
        lifecycle.drain()
    lifecycle.replace_process()


if __name__ == "__main__":
    main()
