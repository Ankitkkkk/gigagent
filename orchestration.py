"""Durable, server-owned selection of workers for original human messages.

The wrapper transport consumes JSONL without delivery acknowledgments. A saved
reservation is therefore never automatically replayed after an uncertain enqueue.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading

log = logging.getLogger(__name__)
_HANDLE = re.compile(r'(?<![\w@])@[A-Za-z0-9_][\w-]*')


def has_explicit_handle(text: str) -> bool:
    """Unknown @handles still express addressing intent; emails do not."""
    return bool(_HANDLE.search(text))


class OrchestrationError(ValueError):
    pass


class OrchestratorService:
    def __init__(self, *, path: Path, workspaces, messages, registry, agents, launcher,
                 allowed_agent=None, routing_paused=None):
        self.path = Path(path)
        self.workspaces, self.messages = workspaces, messages
        self.registry, self.agents, self.launcher = registry, agents, launcher
        self.allowed_agent = allowed_agent or (lambda channel: None)
        self.routing_paused = routing_paused or (lambda channel: False)
        self._lock = threading.RLock()
        self._notified = set()
        self._installed = set()
        self._requests = {}
        self.warning = None
        if self.path.exists():
            try:
                body = json.loads(self.path.read_text('utf-8'))
                if body.get('version') != 1 or not isinstance(body.get('requests'), dict):
                    raise ValueError('unsupported request ledger')
                for key, row in body['requests'].items():
                    if (not isinstance(row, dict) or row.get('status') not in
                            ('pending', 'queued', 'uncertain', 'cancelled') or
                            not isinstance(row.get('message'), dict) or
                            not isinstance(row.get('workspace_id'), str)):
                        raise ValueError('invalid request record')
                self._requests = body['requests']
            except (OSError, ValueError, TypeError, AttributeError) as error:
                # Never replace unreadable decision history: doing so could
                # enqueue a previously dispatched message a second time.
                self.warning = f'Orchestration ledger unavailable: {error}'
                log.error(self.warning)
        if not self.warning:
            with self.launcher._lifecycle_lock, self._lock:
                self._restore_assignments()

    def _restore_assignments(self):
        """Restore unread recovery after reservation-before-routing interruption.

        Workspace assignment tombstones make this safe even after an ACK/prune.
        This never appends to a provider queue.
        """
        for key, request in self._requests.items():
            if key in self._installed or request['status'] not in ('queued', 'uncertain'):
                continue
            ws = self.workspaces.get(request['workspace_id'])
            if not ws or ws.get('archived'):
                continue
            known = {agent['agent_id'] for agent in ws['agents']}
            selected = [ident for ident in request.get('agent_ids', []) if ident in known]
            self.workspaces.record_routing(ws['channel'], request['message']['id'], selected)
            self._installed.add(key)

    def _save(self, requests):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'requests': requests}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)
        self._requests = requests

    def _check(self):
        if self.warning:
            raise OrchestrationError(self.warning)

    @staticmethod
    def _key(ws_id, message_id):
        return f'{ws_id}:{message_id}'

    def configured_workspace(self, channel):
        return next((ws for ws in self.workspaces.list() if ws['channel'] == channel
                     and (ws.get('orchestrator') or {}).get('agent_id')), None)

    def _manager(self, ws):
        control = ws.get('orchestrator') or {}
        return next((agent for agent in ws['agents']
                     if agent['agent_id'] == control.get('agent_id') and
                     agent.get('kind') == 'orchestrator'), None)

    def _ready(self, agent):
        shadow = self.workspaces.read_identity(agent['agent_id']) or {}
        owned = self.registry.resolve_token(shadow.get('token')) if shadow.get('token') else None
        return (agent['last_state'] == 'running' and
                owned is not None and owned['name'] == agent['registry_name'] and
                not (agent.get('last_launch') or {}).get('terminated') and
                self.launcher.startup_ready(agent['registry_name']) and
                self.agents.is_available(agent['registry_name']))

    def _authorize(self, token):
        from workspace_unread import is_blocked
        self._check()
        inst = self.registry.resolve_token(token) if token else None
        found = self.workspaces.find_agent_by_registry_name(inst['name']) if inst else None
        if not found:
            raise OrchestrationError('A registered session orchestrator token is required.')
        ws, agent = found
        shadow = self.workspaces.read_identity(agent['agent_id']) or {}
        manager = self._manager(ws)
        if (ws.get('archived') or not (ws.get('orchestrator') or {}).get('enabled') or
                not manager or manager['agent_id'] != agent['agent_id'] or
                not secrets.compare_digest(str(shadow.get('token', '')), token) or
                is_blocked({'agent_id': agent['agent_id'], 'floor_id': agent.get('floor_id')}) or
                not self._ready(agent)):
            raise OrchestrationError('Only the current ready, enabled session orchestrator can route requests.')
        return ws, agent

    def _workers(self, ws, message=None):
        from workspace_unread import is_blocked, visible
        allowed = self.allowed_agent(ws['channel'])
        workers = []
        for agent in ws['agents']:
            if agent.get('kind', 'worker') != 'worker' or not self._ready(agent):
                continue
            if allowed and agent['registry_name'] != allowed:
                continue
            policy = {'agent_id': agent['agent_id'], 'floor_id': agent.get('floor_id')}
            if is_blocked(policy) or (message is not None and not visible(policy, message)):
                continue
            workers.append(agent)
        return workers

    def submit(self, ws_id, message):
        """Persist before waking. Only the new-message observer calls this."""
        with self.launcher._lifecycle_lock, self._lock:
            self._check()
            ws = self.workspaces.get(ws_id)
            if not ws or ws.get('archived') or not self._manager(ws):
                raise OrchestrationError('Session has no orchestrator.')
            if message.get('channel') != ws['channel'] or message.get('type') != 'chat':
                raise OrchestrationError('Only ordinary messages from this session can be routed.')
            from workspace_unread import visible
            manager = self._manager(ws)
            if not visible({'agent_id': manager['agent_id'], 'floor_id': manager.get('floor_id')}, message):
                raise OrchestrationError('The orchestrator cannot access this message under its history policy.')
            key = self._key(ws_id, message['id'])
            if key not in self._requests:
                rows = copy.deepcopy(self._requests)
                rows[key] = {'workspace_id': ws_id, 'message': copy.deepcopy(message),
                             'status': 'pending', 'agent_ids': [], 'deliveries': {}}
                self._save(rows)
            self._notify(ws)
            return copy.deepcopy(self._requests[key])

    def _notify(self, ws):
        if not (ws.get('orchestrator') or {}).get('enabled') or ws.get('archived'):
            return
        manager = self._manager(ws)
        if not manager or not self._ready(manager) or self.routing_paused(ws['channel']):
            return
        pending = [key for key, request in self._requests.items()
                   if request['workspace_id'] == ws['id'] and request['status'] == 'pending']
        nonce = (manager.get('last_launch') or {}).get('nonce')
        fresh = [(key, nonce) for key in pending if (key, nonce) not in self._notified]
        if not fresh:
            return
        self.agents.trigger_sync(manager['registry_name'], channel=ws['channel'],
            expected_token=self.workspaces.read_identity(manager['agent_id'])['token'],
            message=f'{len(pending)} routing requests pending',
            prompt='Routing requests are pending. Call chat_orchestrate(action="pending") '
                   'for original messages and the current worker roster. Select the most suitable '
                   'workers by role, personality, and name, then call chat_orchestrate(action="route", '
                   'message_id=the_original_id, agent_ids=[chosen_stable_ids], reason="brief reason"). '
                   'Route only; do not implement tasks or send addressed relays. If no suitable ready '
                   'worker exists, leave the request pending. If has_more is true, finish this page '
                   'and read the next pending page. Stop when done; do not poll an empty queue.')
        self._notified.update(fresh)

    def tick(self):
        with self.launcher._lifecycle_lock, self._lock:
            self._check()
            self._restore_assignments()
            for ws in self.workspaces.list():
                self._notify(ws)

    def on_startup_ready(self, ws_id, agent_id):
        # A newly joined worker changes the roster. Wake a waiting manager once
        # for that event, including requests that previously had no eligible worker.
        with self.launcher._lifecycle_lock, self._lock:
            ws = self.workspaces.get(ws_id)
            if ws:
                self._notified = {pair for pair in self._notified
                                  if not pair[0].startswith(ws_id + ':')}
                self._notify(ws)

    def _original(self, request):
        saved = request['message']
        current = self.messages.get_by_id(saved['id'])
        if current is None or current != saved:
            raise OrchestrationError('Original message was changed or deleted; send a new request.')
        return current

    def call(self, token, action='pending', *, message_id=None, agent_ids=None, reason=''):
        with self.launcher._lifecycle_lock, self._lock:
            ws, manager = self._authorize(token)
            if action == 'pending':
                from workspace_unread import visible
                requests = []
                for request in self._requests.values():
                    if request['workspace_id'] != ws['id'] or request['status'] != 'pending':
                        continue
                    try:
                        self._original(request)
                    except OrchestrationError:
                        continue
                    if not visible({'agent_id': manager['agent_id'], 'floor_id': manager.get('floor_id')}, request['message']):
                        continue
                    requests.append(copy.deepcopy(request))
                workers = [{'agent_id': worker['agent_id'], 'name': worker['registry_name'],
                            'provider': worker['provider'], 'profile': worker.get('profile'),
                            'floor_id': worker.get('floor_id', 0)} for worker in self._workers(ws)]
                return {'workspace_id': ws['id'], 'channel': ws['channel'],
                        'requests': requests[:25], 'has_more': len(requests) > 25, 'workers': workers}
            if action != 'route':
                raise OrchestrationError('Use action pending or route.')
            if (type(message_id) is not int or not isinstance(agent_ids, list) or not agent_ids or
                    not all(isinstance(ident, str) for ident in agent_ids) or
                    not isinstance(reason, str) or len(reason) > 500):
                raise OrchestrationError('Provide a message_id, nonempty agent_ids list, and a reason under 500 characters.')
            key = self._key(ws['id'], message_id)
            request = self._requests.get(key)
            if not request:
                raise OrchestrationError('No pending routing request with this ID in your session.')
            original = self._original(request)
            from workspace_unread import visible
            if not visible({'agent_id': manager['agent_id'], 'floor_id': manager.get('floor_id')}, original):
                raise OrchestrationError('The orchestrator cannot access this message under its history policy.')
            selected = sorted(set(agent_ids))
            if request['status'] != 'pending':
                if request['agent_ids'] != selected:
                    raise OrchestrationError('This request already has a different decision.')
                return copy.deepcopy(request)
            if self.routing_paused(ws['channel']):
                raise OrchestrationError('Routing is paused; a human must continue it.')
            eligible = {agent['agent_id']: agent for agent in self._workers(ws, original)}
            if any(ident not in eligible for ident in selected):
                raise OrchestrationError('Select only ready workers from this session with access to the original message.')
            rows = copy.deepcopy(self._requests)
            reserved = rows[key]
            reserved.update(status='uncertain', agent_ids=selected, reason=reason,
                            manager_id=manager['agent_id'],
                            deliveries={ident: 'uncertain' for ident in selected})
            self._save(rows)  # Never append to a transport without a durable reservation.
            self.workspaces.record_routing(ws['channel'], message_id, selected)
            self._installed.add(key)
            for ident in selected:
                worker = eligible[ident]
                try:
                    self.agents.trigger_sync(worker['registry_name'], channel=ws['channel'],
                        expected_token=self.workspaces.read_identity(worker['agent_id'])['token'],
                        message=f"{original['sender']}: {original['text']}",
                        prompt=f"The session orchestrator assigned original message #{message_id} to you. "
                               f"Use chat_read(sender={json.dumps(worker['registry_name'])}, "
                               f"channel={json.dumps(ws['channel'])}, since_id={message_id - 1}) "
                               'to read the request and attachments. Work within your saved role and '
                               'personality, and respond in the same channel with chat_send. '
                               f"Original request: {original['text']}")
                except Exception:
                    log.exception('Uncertain orchestration enqueue for %s message %s', ident, message_id)
                else:
                    rows = copy.deepcopy(self._requests)
                    rows[key]['deliveries'][ident] = 'queued'
                    self._save(rows)
            rows = copy.deepcopy(self._requests)
            result = rows[key]
            if all(state == 'queued' for state in result['deliveries'].values()):
                result['status'] = 'queued'
            self._save(rows)
            names = ', '.join(eligible[ident]['registry_name'] for ident in selected)
            note = (f'Orchestrator queued message #{message_id} for {names}.' if result['status'] == 'queued'
                    else f'Orchestrator dispatch for message #{message_id} is uncertain. '
                         'Review worker unread messages and use Retry deliberately.')
            self.messages.add('system', note, msg_type='system', channel=ws['channel'],
                              reply_to=message_id, metadata={'orchestration': key})
            return copy.deepcopy(result)
