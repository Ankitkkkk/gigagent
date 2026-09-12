"""Workspace HTTP facade and user-facing session/agent resolution."""

import cli_api
from cli_api import CLIError
from urllib.parse import quote, urlencode


def _safe(value):
    return "".join(char for char in str(value)
                   if char.isprintable() and char != "\x1b")


def _session_choices(items):
    return ", ".join(f"{_safe(item.get('name', ''))} ({_safe(item.get('id', ''))})"
                     for item in items)


def resolve_session(items, selector):
    """Resolve an exact id, exact name, or unique name prefix."""
    by_id = [item for item in items if item.get("id") == selector]
    if len(by_id) == 1:
        return by_id[0]
    by_name = [item for item in items if item.get("name") == selector]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise CLIError(f"ambiguous session {_safe(selector)}: {_session_choices(by_name)}")
    by_prefix = [item for item in items
                 if str(item.get("name", "")).startswith(selector)]
    if len(by_prefix) == 1:
        return by_prefix[0]
    if len(by_prefix) > 1:
        raise CLIError(f"ambiguous session {_safe(selector)}: {_session_choices(by_prefix)}")
    raise CLIError(f"session not found: {_safe(selector)}")


def _agent_choices(items):
    return ", ".join(f"{_safe(item.get('registry_name', ''))} "
                     f"({_safe(item.get('agent_id', ''))})" for item in items)


def resolve_agent(workspace, selector):
    """Resolve an agent inside one workspace."""
    agents = workspace.get("agents", [])
    exact = [agent for agent in agents if selector in
             (agent.get("registry_name"), agent.get("agent_id"))]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise CLIError(f"ambiguous agent {_safe(selector)}: {_agent_choices(exact)}")
    by_provider = [agent for agent in agents if agent.get("provider") == selector]
    if len(by_provider) == 1:
        return by_provider[0]
    if len(by_provider) > 1:
        raise CLIError(f"ambiguous agent {_safe(selector)}: {_agent_choices(by_provider)}")
    raise CLIError(f"agent not found: {_safe(selector)}")


class WorkspaceAPI:
    def __init__(self, url, timeout=15):
        self.url = cli_api.local_url(url)
        self.timeout = timeout
        self._token = None

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - cli_api.monotonic()
        if remaining <= 0:
            raise CLIError("Request timed out")
        return remaining

    def request(self, method, path, body=None):
        deadline = cli_api.monotonic() + self.timeout
        if self._token is None:
            try:
                self._token = cli_api.fetch_session_token(
                    self.url, timeout=self._remaining(deadline))
            except CLIError:
                raise
            except OSError:
                raise CLIError("Could not connect to the local agentchattr server") from None
            except ValueError as error:
                raise CLIError(str(error)) from None
        try:
            return cli_api.request_json(
                self.url, self._token, method, path, body=body,
                timeout=self._remaining(deadline))
        except CLIError as error:
            if error.status in (401, 403):
                self._token = None
            raise

    def list(self, include_archived=False):
        query = urlencode({"include_archived": "1" if include_archived else "0"})
        return self.request("GET", "/api/workspaces?" + query)

    def get(self, ws_id):
        return self.request("GET", "/api/workspaces/" + quote(ws_id, safe=""))

    def resolve(self, selector, include_archived=True):
        return resolve_session(self.list(include_archived)["workspaces"], selector)

    def create(self, name=""):
        return self.request("POST", "/api/workspaces", {"name": name})

    def rename(self, ws_id, name):
        root = "/api/workspaces/" + quote(ws_id, safe="")
        return self.request("PATCH", root, {"name": name})

    def action(self, ws_id, action, agent_id=None, body=None):
        root = "/api/workspaces/" + quote(ws_id, safe="")
        if action == "spawn":
            path = root + "/agents"
        elif agent_id is None:
            path = root + "/" + action
        else:
            path = root + "/agents/" + quote(agent_id, safe="") + "/" + action
        if action in ("resume", "history") and body is None:
            body = {}
        elif action in ("stop", "retry"):
            body = None
        return self.request("POST", path, body)

    def unread(self, ws_id, agent_id=None):
        path = "/api/workspaces/" + quote(ws_id, safe="") + "/unread"
        if agent_id is not None:
            path += "?" + urlencode({"agent_id": agent_id})
        return self.request("GET", path)

    def status(self):
        return self.request("GET", "/api/status")
