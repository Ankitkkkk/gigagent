"""Interactive chat and shell commands for a running local agentchattr server."""

import argparse
import asyncio
from collections import OrderedDict
from contextlib import redirect_stdout
import json
import re
import sys
import uuid
from urllib.error import URLError
from urllib.parse import urlencode

from cli_api import (CLIError, SessionTokenParser, fetch_session_token, get_api,
                     local_url)
from cli_workspaces import (WorkspaceAPI, format_workspace_result,
                            run_workspace_command)
from config_loader import load_config


HELP = """/channels           List channels
/join NAME          Switch to an existing channel
/create NAME        Create a channel
/agents             Show agent availability and roles
/history            Show recent messages in this channel
/jobs               List jobs
/rules              List rules
/help               Show commands
/quit               Disconnect
Type a message to send it; @mentions wake agents. Tab completes names.
Server commands such as /continue and /summary @agent are sent to chat."""


def terminal_text(value):
    """Keep chat content from emitting terminal control sequences."""
    return "".join(c for c in str(value) if c in "\n\t" or
                   (c.isprintable() and c != "\x1b"))


class ChatClient:
    def __init__(self, url, channel="general", username=None, history_limit=30,
                 output=print):
        self.url = local_url(url)
        self.channel = channel.removeprefix("#")
        self.username = username
        self.history_limit = history_limit
        self.output = output
        self.channels = []
        self.status = {}
        self.agent_names = []
        self.jobs = []
        self.rules = []
        self.messages = OrderedDict()
        self.websocket = None
        self.ready = asyncio.Event()
        self.pending_channel = None

    def show(self, text):
        self.output(terminal_text(text))

    def remember(self, message):
        key = message["id"]
        fresh = key not in self.messages
        self.messages[key] = message
        while len(self.messages) > 10000:
            self.messages.popitem(last=False)
        return fresh

    def show_message(self, message):
        self.show(f"[{message.get('time', '')}] {message.get('sender', '?')}: "
                  f"{message.get('text', '')}")
        for attachment in message.get("attachments", []):
            self.show(f"  Attachment: {attachment.get('url') or attachment.get('name', 'image')}")
        choices = message.get("metadata", {}).get("choices", [])
        if choices:
            self.show("  Choices: " + " | ".join(map(str, choices)))

    def history(self):
        messages = sorted((m for m in self.messages.values()
                           if m.get("channel", "general") == self.channel),
                          key=lambda m: (m.get("timestamp", 0), m["id"]))
        self.show(f"# {self.channel}")
        for message in messages[-self.history_limit:]:
            self.show_message(message)

    def handle_event(self, event):
        kind = event.get("type")
        data = event.get("data", {})
        if kind == "settings":
            self.channels = data.get("channels", ["general"])
            if self.username is None:
                self.username = data.get("username", "user")
            if self.pending_channel in self.channels:
                self.channel = self.pending_channel
                self.pending_channel = None
                self.history()
            elif self.channel not in self.channels:
                self.show(f"Channel {self.channel!r} is unavailable; using #general.")
                self.channel = "general"
        elif kind == "agents":
            self.agent_names = list(data)
        elif kind == "status":
            self.status = data
        elif kind == "jobs":
            self.jobs = data
        elif kind == "rules":
            self.rules = data
        elif kind in ("job", "rule"):
            records = self.jobs if kind == "job" else self.rules
            records[:] = [r for r in records if r["id"] != data.get("id")]
            if event.get("action") != "delete" and "id" in data:
                records.append(data)
        elif kind == "history":
            for message in event.get("messages", []):
                self.remember(message)
        elif kind == "history_complete":
            self.history()
            self.ready.set()
        elif kind == "message":
            fresh = self.remember(data)
            if fresh and self.ready.is_set() and data.get("channel", "general") == self.channel:
                self.show_message(data)
        elif kind == "message_update":
            self.remember(event["message"])
        elif kind == "delete":
            for key in event.get("ids", []):
                self.messages.pop(key, None)
        elif kind == "clear":
            channel = event.get("channel")
            self.messages = OrderedDict((k, m) for k, m in self.messages.items()
                                        if channel and m.get("channel", "general") != channel)
            self.show(f"History cleared: #{channel or 'all channels'}")
        elif kind == "agent_renamed":
            for message in self.messages.values():
                if message.get("sender") == event.get("old_name"):
                    message["sender"] = event["new_name"]
        elif kind == "channel_renamed":
            for message in self.messages.values():
                if message.get("channel") == event["old_name"]:
                    message["channel"] = event["new_name"]
        elif kind.endswith("_error"):
            self.show(event.get("error", "Request failed"))

    async def receive_forever(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        while True:
            try:
                token = await asyncio.to_thread(fetch_session_token, self.url)
                uri = "ws" + self.url[4:] + "/ws?" + urlencode({"token": token})
                async with connect(uri, proxy=None, open_timeout=5,
                                   close_timeout=2, max_size=16 * 1024 * 1024) as socket:
                    self.websocket = socket
                    self.messages.clear()
                    self.show(f"Connected to {self.url}")
                    async for raw in socket:
                        self.handle_event(json.loads(raw))
            except (OSError, ValueError, TimeoutError, WebSocketException):
                # Exception URLs may contain the token; never print them.
                self.show("Connection unavailable. Start run.py; retrying in 3 seconds.")
            finally:
                self.websocket = None
                self.ready.clear()
            await asyncio.sleep(3)

    async def send(self, event):
        from websockets.exceptions import WebSocketException

        if not self.ready.is_set() or self.websocket is None:
            self.show("Disconnected. Message was not sent.")
            return False
        try:
            await self.websocket.send(json.dumps(event))
            return True
        except (OSError, WebSocketException):
            self.show("Connection lost. Delivery is uncertain; check history before retrying.")
            return False

    async def submit(self, text):
        text = text.strip()
        if not text:
            return True
        command, _, argument = text.partition(" ")
        argument = argument.strip().removeprefix("#")
        if command in ("/quit", "/exit"):
            return False
        if command == "/help":
            self.show(HELP)
        elif command == "/channels":
            self.show("  ".join("#" + ch for ch in self.channels) or "Connecting...")
        elif command == "/join":
            if argument not in self.channels:
                self.show("Unknown channel. Use /channels or /create NAME.")
            else:
                self.channel = argument
                self.history()
        elif command == "/create":
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", argument):
                self.show("Use 1-20 lowercase letters, numbers, or hyphens; start with a letter or number.")
            elif argument in self.channels:
                self.channel = argument
                self.history()
            else:
                self.pending_channel = argument
                if not await self.send({"type": "channel_create", "name": argument}):
                    self.pending_channel = None
        elif command == "/history":
            self.history()
        elif command == "/agents":
            for name, info in self.status.items():
                if isinstance(info, dict):
                    state = "busy" if info.get("busy") else "online" if info.get("available") else "offline"
                    self.show(f"@{name}: {state}" + (f" ({info['role']})" if info.get("role") else ""))
            if self.status.get("paused"):
                self.show("An agent conversation is paused by the loop guard.")
        elif command in ("/jobs", "/rules"):
            records = self.jobs if command == "/jobs" else self.rules
            for record in records:
                self.show(f"{record['id']} [{record.get('status', '')}] "
                          f"{record.get('title', record.get('text', ''))}")
            if not records:
                self.show("No records.")
        else:
            await self.send({"type": "message", "text": text,
                             "channel": self.channel, "sender": self.username})
        return True


async def interactive(client):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.patch_stdout import patch_stdout

    commands = ["/channels", "/join", "/create", "/agents", "/history",
                "/jobs", "/rules", "/help", "/quit", "/continue", "/summary"]
    session = PromptSession(completer=WordCompleter(
        lambda: commands + ["@" + n for n in client.agent_names] + client.channels,
        WORD=True))
    with patch_stdout():
        client.show("agentchattr terminal | /help for commands | /quit to exit")
        receiver = asyncio.create_task(client.receive_forever())
        try:
            while True:
                try:
                    text = await session.prompt_async(lambda: f"#{terminal_text(client.channel)} > ")
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    break
                if not await client.submit(text):
                    break
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


async def shell_command(client, args):
    from websockets.asyncio.client import connect

    try:
        token = await asyncio.to_thread(fetch_session_token, client.url)
    except (OSError, URLError):
        raise CLIError("Could not connect to the local agentchattr server") from None
    if args.command == "status":
        return await asyncio.to_thread(get_api, client.url, token, "/api/status")
    settings = await asyncio.to_thread(get_api, client.url, token, "/api/settings")
    client.handle_event({"type": "settings", "data": settings})
    requested_channel = args.channel.removeprefix("#")
    if args.command == "channels":
        return settings["channels"]
    if requested_channel not in settings["channels"]:
        raise ValueError(f"Unknown channel: {requested_channel}")
    if args.command == "read":
        query = urlencode({"channel": client.channel, "limit": args.history})
        return await asyncio.to_thread(get_api, client.url, token, "/api/messages?" + query)

    text = " ".join(args.message).strip()
    if not text:
        raise ValueError("Message must not be empty")
    request_id = uuid.uuid4().hex
    uri = "ws" + client.url[4:] + "/ws?" + urlencode({"token": token})
    async with connect(uri, proxy=None, open_timeout=5, close_timeout=2,
                       max_size=16 * 1024 * 1024) as socket:
        async for raw in socket:
            event = json.loads(raw)
            if event.get("type") == "history_complete":
                await socket.send(json.dumps({"type": "message", "text": text,
                    "sender": client.username, "channel": client.channel,
                    "request_id": request_id}))
            elif event.get("type") == "message_sent" and event.get("request_id") == request_id:
                return event["data"]
    raise ValueError("Connection closed before the server confirmed delivery")


def build_parser():
    parser = argparse.ArgumentParser(description="Terminal chat and shell commands for agentchattr.")
    parser.set_defaults(url=None, channel=None, session=None, name=None, history=30,
                        timeout=15, json=False, command="chat", agent_name=None,
                        history_mode="literal", cwd=None, fresh=False, archived=False,
                        yes=False, agent=None, no_resume=False, provider=None,
                        session_name=None, target_session=None)

    def options(target):
        target.add_argument("--url", default=argparse.SUPPRESS, help="Local server URL")
        target.add_argument("--channel", default=argparse.SUPPRESS, help="Channel (default: general)")
        target.add_argument("--session", default=argparse.SUPPRESS,
                            help="Session id, name, or unique name prefix")
        target.add_argument("--name", default=argparse.SUPPRESS, help="Human display name")
        target.add_argument("--history", "--limit", type=int, default=argparse.SUPPRESS,
                            help="Recent messages to display (default: 30)")
        target.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                            help="Shell command timeout in seconds (default: 15)")
        target.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                            help="Machine-readable output for shell commands")

    options(parser)
    commands = parser.add_subparsers(dest="command")
    command_help = [("chat", "Interactive chat (default)"),
                    ("send", "Send a message; use - to read stdin"),
                    ("read", "Read recent channel messages"),
                    ("channels", "List channels"),
                    ("status", "Show agent status"),
                    ("sessions", "List terminal sessions"),
                    ("new", "Create a terminal session"),
                    ("spawn", "Start a new agent in a session"),
                    ("resume", "Resume an agent in a session"),
                    ("stop", "Stop an agent in a session"),
                    ("unread", "Show unread messages for session agents"),
                    ("retry", "Retry unread delivery for an agent"),
                    ("history", "Change an agent history mode"),
                    ("archive", "Archive a terminal session")]
    for command, help_text in command_help:
        subparser = commands.add_parser(command, help=help_text)
        options(subparser)
        if command == "chat":
            subparser.add_argument("--no-resume", action="store_true",
                                   default=argparse.SUPPRESS,
                                   help="Do not offer to resume stopped agents")
        elif command == "send":
            subparser.add_argument("message", nargs="+", help="Message text or - for stdin")
        elif command == "sessions":
            subparser.add_argument("--archived", action="store_true",
                                   default=argparse.SUPPRESS,
                                   help="Include archived sessions")
        elif command == "new":
            subparser.add_argument("session_name", metavar="NAME")
        elif command == "spawn":
            subparser.add_argument("provider", metavar="PROVIDER")
            subparser.add_argument("--cwd", required=True)
            subparser.add_argument("--agent-name", default=argparse.SUPPRESS)
            subparser.add_argument("--history-mode", default=argparse.SUPPRESS,
                                   metavar="MODE")
        elif command == "resume":
            subparser.add_argument("agent", metavar="AGENT")
            subparser.add_argument("--fresh", action="store_true",
                                   default=argparse.SUPPRESS)
            subparser.add_argument("--agent-name", default=argparse.SUPPRESS)
            subparser.add_argument("--cwd", default=argparse.SUPPRESS)
        elif command in ("stop", "retry"):
            subparser.add_argument("agent", metavar="AGENT")
        elif command == "unread":
            subparser.add_argument("--agent", default=argparse.SUPPRESS)
        elif command == "history":
            subparser.add_argument("agent", metavar="AGENT")
            subparser.add_argument("--mode", dest="history_mode", required=True,
                                   metavar="MODE")
        elif command == "archive":
            subparser.add_argument("target_session", metavar="SESSION")
            subparser.add_argument("--yes", action="store_true",
                                   default=argparse.SUPPRESS)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.command = args.command or "chat"
    if not 1 <= args.history <= 10000:
        parser.error("--history must be between 1 and 10000")
    if not 0 < args.timeout <= 300:
        parser.error("--timeout must be between 0 and 300 seconds")
    if args.channel is not None and args.session is not None:
        parser.error("--channel and --session cannot be used together")
    if args.command == "archive" and args.session is not None:
        parser.error("archive SESSION cannot be combined with --session")
    if args.command in ("spawn", "resume", "stop", "unread", "retry", "history") \
            and args.session is None:
        parser.error(f"{args.command} requires --session")
    if args.history_mode == "summary":
        parser.error("summary history mode is not available in this version; use literal or none")
    if args.command in ("spawn", "history") and args.history_mode not in ("literal", "none"):
        parser.error("history mode must be literal or none")
    if args.command == "archive" and not args.yes:
        if not sys.stdin.isatty():
            parser.exit(1, "archive requires --yes when input is not a terminal\n")
        if input("Archive session? [y/N] ").strip().lower() not in ("y", "yes"):
            return
    workspace_commands = {"sessions", "new", "spawn", "resume", "stop",
                          "unread", "retry", "history", "archive"}
    try:
        if args.command in ("chat", "send", "read", "status", "channels"):
            from websockets.asyncio.client import connect  # noqa: F401
        if args.command == "chat":
            import prompt_toolkit  # noqa: F401
    except ImportError:
        parser.exit(1, "Install terminal dependencies: python -m pip install -r requirements-cli.txt\n")
    if args.command == "chat":
        if args.json:
            parser.error("--json is only available for send, read, channels, and status")
        if not sys.stdin.isatty():
            parser.exit(1, "Interactive chat requires a terminal. Use read or send for scripts.\n")
    elif args.command == "send" and args.message == ["-"]:
        args.message = [sys.stdin.read()]
    with redirect_stdout(sys.stderr):
        url = args.url or f"http://127.0.0.1:{load_config()['server']['port']}"
    try:
        output = print if args.command == "chat" else lambda text: None
        channel = (args.channel or "general").removeprefix("#")
        client = ChatClient(url, channel, args.name, args.history, output=output)
    except ValueError as error:
        parser.error(str(error))
    try:
        if args.command == "chat":
            asyncio.run(interactive(client))
        elif args.command in workspace_commands:
            api = WorkspaceAPI(url, timeout=args.timeout)

            async def run_session_command():
                async with asyncio.timeout(args.timeout):
                    return await asyncio.to_thread(run_workspace_command, api, args)

            result = asyncio.run(run_session_command())
            if args.json:
                print(json.dumps(result.data, ensure_ascii=True))
            else:
                print(terminal_text(format_workspace_result(args.command, result)))
        else:
            async def run_command():
                async with asyncio.timeout(args.timeout):
                    if args.session:
                        api = WorkspaceAPI(url, timeout=args.timeout)
                        workspace = await asyncio.to_thread(
                            api.resolve, args.session, True)
                        args.channel = workspace["channel"]
                        client.channel = workspace["channel"]
                    else:
                        args.channel = channel
                    return await shell_command(client, args)
            result = asyncio.run(run_command())
            if args.json:
                print(json.dumps(result, ensure_ascii=True))
            elif args.command == "channels":
                print(terminal_text("\n".join("#" + ch for ch in result)))
            elif args.command == "read":
                client.output = print
                for message in result:
                    client.show_message(message)
            elif args.command == "status":
                client.output = print
                client.status = result
                asyncio.run(client.submit("/agents"))
            else:
                print(f"Sent to #{terminal_text(client.channel)}" +
                      (f" (message {result['id']})" if "id" in result else ""))
    except KeyboardInterrupt:
        parser.exit(130)
    except ValueError as error:
        message = terminal_text(str(error))
        if args.command != "chat" and ("Could not connect" in message or
                                        "timed out" in message.lower()):
            message += "\nStart it manually: python run.py"
        parser.exit(1, message + "\n")
    except TimeoutError:
        parser.exit(1, "Server request timed out.\nStart it manually: python run.py\n")
    except Exception:
        # Transport exception messages may embed the session token in a URL.
        parser.exit(1, "Server request failed or timed out. Check run.py and --url. "
                    "For send, delivery may be uncertain; check history before retrying.\n")


if __name__ == "__main__":
    main()
