"""Local authenticated HTTP helpers shared by agentchattr CLI commands."""

from html.parser import HTMLParser
import json
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class CLIError(ValueError):
    """A user-facing CLI request error with an optional HTTP status."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.message = message
        self.status = status


def local_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in
            ("localhost", "127.0.0.1", "::1") or parsed.username or
            parsed.password or parsed.path not in ("", "/") or
            parsed.query or parsed.fragment):
        raise ValueError("Use a local server URL such as http://127.0.0.1:8300")
    _ = parsed.port
    return value.rstrip("/")


class SessionTokenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_script = False
        self.parts = []
        self.token = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            self.parts = []

    def handle_data(self, data):
        if self.in_script:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            script = "".join(self.parts).strip()
            prefix = "window.__SESSION_TOKEN__="
            if script.startswith(prefix):
                token, _ = json.JSONDecoder().raw_decode(script[len(prefix):])
                if isinstance(token, str) and token:
                    self.token = token
            self.in_script = False


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return build_opener(ProxyHandler({}), _NoRedirectHandler())


def fetch_session_token(url, timeout=5):
    """Fetch browser bootstrap token without persisting it."""
    url = local_url(url)
    try:
        with _opener().open(url + "/", timeout=timeout) as response:
            if response.geturl().rstrip("/") != url:
                raise ValueError("Unexpected redirect from the local server")
            parser = SessionTokenParser()
            parser.feed(response.read().decode("utf-8"))
    except HTTPError as error:
        if 300 <= error.code < 400:
            raise ValueError("Unexpected redirect from the local server") from None
        raise
    if not parser.token:
        raise ValueError("The server did not provide an agentchattr session token")
    return parser.token


def _error_message(error):
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return f"Request failed (HTTP {error.code})"
    if isinstance(payload, dict):
        detail = payload.get("error", payload.get("detail"))
        if isinstance(detail, str) and detail:
            return detail
        if detail is not None:
            return json.dumps(detail, ensure_ascii=False, sort_keys=True)
    return f"Request failed (HTTP {error.code})"


def request_json(url, token, method, path, body=None, timeout=5):
    """Send one authenticated request and decode its JSON response."""
    url = local_url(url)
    method = method.upper()
    headers = {"X-Session-Token": token, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method in ("POST", "PATCH", "PUT"):
        data = b""
    else:
        data = None
    request = Request(url + path, data=data, headers=headers, method=method)
    try:
        with _opener().open(request, timeout=timeout) as response:
            if response.geturl().split("#", 1)[0] != request.full_url:
                raise CLIError("Unexpected redirect from the local server")
            raw = response.read()
    except HTTPError as error:
        if 300 <= error.code < 400:
            raise CLIError("Unexpected redirect from the local server", error.code) from None
        raise CLIError(_error_message(error), error.code) from None
    except (URLError, OSError, TimeoutError):
        raise CLIError("Could not connect to the local agentchattr server") from None
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CLIError("The local server returned an invalid JSON response") from None


def get_api(url, token, path):
    """Compatibility wrapper for existing JSON GET callers."""
    return request_json(url, token, "GET", path)
