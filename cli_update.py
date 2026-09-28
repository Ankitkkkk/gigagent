"""`yapp update`: check for a newer release, install it, restart the local server."""

import json
import sys
from pathlib import Path

import updates
from cli_api import CLIError, local_url
from cli_workspaces import WorkspaceAPI


def _default_error_output(text):
    print(text, file=sys.stderr)


def _same_data_dir(api, data_dir):
    """True only when the server says it uses this install's data_dir (as ensure_server checks)."""
    try:
        running = api.status().get('data_dir')
        return (isinstance(running, str) and bool(running)
                and Path(running).resolve() == Path(data_dir).resolve())
    except (CLIError, OSError, ValueError, AttributeError):
        return False


def _restart_local_server(url, api_factory, data_dir):
    try:
        api = api_factory(url)
        status = api.server_status()
    except (CLIError, OSError, ValueError):
        return 'No local server is running; the new version starts next time you run yapp.'
    if not _same_data_dir(api, data_dir):
        return f'A yapp server from a different install is running on {url}; not restarting it.'
    if not status.get('restart_supported') or status.get('state') != 'ready':
        return 'Restart the yapp server to finish the update (F4 → Restart server in yapp).'
    try:
        api.restart_server(status['instance_id'])
    except (CLIError, OSError) as error:
        return f'Could not restart the server ({error}); restart it from yapp with F4 → Restart server.'
    return 'Server restarted. Open yapp windows reopen on the new version automatically.'


def run_update(args, config, *, output=print, error_output=_default_error_output, ask=input,
               stdin_tty=sys.stdin.isatty, check=updates.check, apply=updates.apply,
               install_method=updates.install_method, api_factory=WorkspaceAPI):
    if args.url:
        try:
            local_url(args.url)
        except ValueError as error:
            error_output(str(error))
            return 1
    # With --json (outside --check), stdout must be exactly one JSON document, so human
    # text goes to error_output and an unattended run needs --yes to avoid a stdin prompt.
    json_mode = args.json and not args.check
    if json_mode and not args.yes:
        error_output('yapp update --json requires --yes')
        return 1
    say = error_output if json_mode else output

    data_dir = updates.data_dir_from_config(config)
    result = check(data_dir=data_dir, config=config, force=True, explicit=True)
    if args.check:
        if args.json:
            output(json.dumps(result))
        elif result['state'] == 'update_available':
            output(f"yapp {result['latest']} is available (you have {result['current']}): {result['url']}")
        elif result['state'] == 'current':
            output(f"yapp {result['current']} is up to date.")
        else:
            output(f"Could not check for updates: {result['error']}")
        return {'update_available': 10, 'current': 0}.get(result['state'], 1)

    outcome = None
    server = None

    def finish(code):
        if json_mode:
            output(json.dumps({'check': result, 'outcome': outcome, 'server': server}))
        return code

    if result['state'] == 'current':
        say(f"yapp {result['current']} is up to date.")
        return finish(0)
    if result['state'] != 'update_available':
        say(f"Could not check for updates: {result['error']}")
        return finish(1)
    method = install_method()
    if method not in ('installer', 'pipx'):
        say(updates.manual_instructions(method))
        return finish(1)
    say(f"yapp {result['latest']} is available (you have {result['current']}).\n"
        f"Release notes: {result['url']}")
    if not args.yes:
        if not stdin_tty():
            say('Run yapp update --yes to update without a terminal.')
            return finish(1)
        if ask('Update now? [y/N] ').strip().lower() not in ('y', 'yes'):
            return finish(0)
    say(f"Installing yapp {result['latest']}…")
    outcome = apply(result, method=method, data_dir=data_dir)
    if not outcome['ok']:
        say(outcome['message'])
        return finish(1)
    say(outcome['message'])
    url = args.url or f"http://127.0.0.1:{config.get('server', {}).get('port', 8300)}"
    server = _restart_local_server(url, api_factory, data_dir)
    say(server)
    return finish(0)
