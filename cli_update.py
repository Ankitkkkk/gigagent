"""`yapp update`: check for a newer release, install it, restart the local server."""

import json
import sys

import updates
from cli_api import CLIError
from cli_workspaces import WorkspaceAPI


def _restart_local_server(url, api_factory, output):
    try:
        api = api_factory(url)
        status = api.server_status()
    except (CLIError, OSError):
        output('No local server is running; the new version starts next time you run yapp.')
        return
    if not status.get('restart_supported') or status.get('state') != 'ready':
        output('Restart the yapp server to finish the update (F4 → Restart server in yapp).')
        return
    try:
        api.restart_server(status['instance_id'])
    except (CLIError, OSError) as error:
        output(f'Could not restart the server ({error}); restart it from yapp with F4 → Restart server.')
        return
    output('Server restarted. Open yapp windows reopen on the new version automatically.')


def run_update(args, config, *, output=print, ask=input, stdin_tty=sys.stdin.isatty,
               check=updates.check, apply=updates.apply, install_method=updates.install_method,
               api_factory=WorkspaceAPI):
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
    if result['state'] == 'current':
        output(f"yapp {result['current']} is up to date.")
        return 0
    if result['state'] != 'update_available':
        output(f"Could not check for updates: {result['error']}")
        return 1
    method = install_method()
    if method not in ('installer', 'pipx'):
        output(updates.manual_instructions(method))
        return 1
    output(f"yapp {result['latest']} is available (you have {result['current']}).\n"
           f"Release notes: {result['url']}")
    if not args.yes:
        if not stdin_tty():
            output('Run yapp update --yes to update without a terminal.')
            return 1
        if ask('Update now? [y/N] ').strip().lower() not in ('y', 'yes'):
            return 0
    output(f"Installing yapp {result['latest']}…")
    outcome = apply(result, method=method, data_dir=data_dir)
    if args.json:
        output(json.dumps(outcome))
    if not outcome['ok']:
        output(outcome['message'])
        return 1
    output(outcome['message'])
    url = args.url or f"http://127.0.0.1:{config.get('server', {}).get('port', 8300)}"
    _restart_local_server(url, api_factory, output)
    return 0
