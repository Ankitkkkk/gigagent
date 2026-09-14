"""Provider-neutral, advisory waiting signals. Never an approval protocol."""
from dataclasses import dataclass
import json
from itertools import islice
from pathlib import Path
import re
import time


def terminal_lines(output: bytes) -> list[str]:
    text = output.decode('utf-8', errors='replace')
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return [line.strip(' \t│┃║') for line in text.splitlines() if line.strip(' \t│┃║')]


def generic_waiting_for_input(output: bytes) -> bool:
    lines = terminal_lines(output)
    if not lines:
        return False
    footer = lines[-1].lower()
    if re.search(r'(?:press\s+)?enter\s+to\s+(?:confirm|select|continue|accept)', footer):
        return bool(re.search(r'(?:esc|escape)\s+to\s+(?:cancel|exit|go back)', footer))
    if re.search(r'\[(?:y/n|n/y)\]|\((?:y/n|n/y)\)', footer):
        return True
    tail = '\n'.join(lines[-8:]).lower()
    return bool(
        re.search(r'(?:esc|escape)\s+to\s+cancel', footer)
        and re.search(r'(?:do you want|would you like|allow|trust|proceed|continue).*\?', tail)
        and re.search(r'(?:^|\n)[^\w\n]*\d+[.)]\s+yes\b', tail)
        and re.search(r'(?:^|\n)[^\w\n]*\d+[.)]\s+no\b', tail)
    )


@dataclass(frozen=True)
class PromptEvent:
    kind: str  # requested, resolved, turn_end, session_end
    session_id: str
    turn_id: str = ''
    tool_name: str = ''

    def valid(self):
        if self.kind not in ('requested', 'resolved', 'turn_end', 'session_end'):
            return False
        if any(not isinstance(value, str) or len(value) > 256
               for value in (self.session_id, self.turn_id, self.tool_name)):
            return False
        return bool(self.session_id and (self.kind == 'session_end' or self.turn_id)
                    and (self.kind not in ('requested', 'resolved') or self.tool_name))


class PromptMonitor:
    """Merge launch-local hook events with visible terminal prompts.

    PermissionRequest has no stable tool-call ID. Correlation is conservative
    per session/turn/tool; it must never be used to select or answer a request.
    """
    HOOK_TIMEOUT = 60

    def __init__(self, adapter, events_dir: Path | None = None, *, clock=time.time):
        self.adapter, self.events_dir, self.clock = adapter, events_dir, clock
        self.pending = []
        self.saw_prompt = False

    def _drain(self, now):
        requested = False
        if self.events_dir is None:
            return requested
        try:
            paths = sorted(islice(self.events_dir.glob('*.json'), 256))
        except OSError:
            return requested
        for path in paths:
            try:
                if path.is_symlink() or path.stat().st_size > 4096:
                    continue
                data = json.loads(path.read_text())
                event = PromptEvent(**data['event'])
                at = data['at']
                # A producer can publish after this poll sampled its clock.
                if not event.valid() or not isinstance(at, (int, float)) or not -1 <= now - at < self.HOOK_TIMEOUT:
                    continue
                at = min(at, now)
                key = (event.session_id, event.turn_id, event.tool_name)
                if event.kind == 'requested':
                    self.pending.append((key, at))
                    self.pending = self.pending[-256:]
                    requested = True
                elif event.kind == 'resolved':
                    index = next((i for i, (k, _) in enumerate(self.pending) if k == key), None)
                    if index is not None:
                        self.pending.pop(index)
                else:
                    size = 1 if event.kind == 'session_end' else 2
                    self.pending = [(k, at) for k, at in self.pending if k[:size] != key[:size]]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        return requested

    def observe(self, output: bytes | None) -> bool:
        now = self.clock()
        requested = self._drain(now)
        self.pending = [(key, at) for key, at in self.pending if 0 <= now - at < self.HOOK_TIMEOUT]
        if output is None:
            return False  # capture failure is not evidence of an actionable prompt
        visible = self.adapter.waiting_for_input(output)
        if self.saw_prompt and not visible and not requested:
            self.pending.clear()  # approval resolved; tool may still be executing
        self.saw_prompt = visible
        return bool(visible or self.pending)
