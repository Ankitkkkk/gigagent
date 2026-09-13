"""Bounded presentation state for the full-screen terminal client."""

from collections import OrderedDict, deque
from dataclasses import dataclass, field

from prompt_toolkit.utils import get_cwidth

from cli_view_contracts import _safe, terminal_text


MAX_DRAFTS = 50
MAX_DRAFT_BYTES = 64 * 1024
MAX_NOTICE_LINES = 1000
MAX_VIEWPORT_IDS = 10000


def label_text(value):
    """Return one terminal-safe label."""
    return _safe(value)


def body_text(value):
    """Return terminal-safe multiline text with fixed-width tabs."""
    return terminal_text(value).expandtabs(4)


def _clusters(text):
    clusters = []
    for character in text:
        if clusters and (get_cwidth(character) == 0 or clusters[-1][-1] == '\u200d'):
            clusters[-1] += character
        else:
            clusters.append(character)
    return clusters


def clip_cells(text, width):
    """Clip text to terminal cells, preserving combining clusters."""
    text = str(text)
    if width <= 0:
        return ''
    clusters = _clusters(text)
    cell_widths = [sum(max(0, get_cwidth(character)) for character in cluster)
                   for cluster in clusters]
    if sum(cell_widths) <= width:
        return text
    ellipsis = '…'
    ellipsis_width = get_cwidth(ellipsis)
    if width < ellipsis_width:
        return ''
    kept = []
    used = 0
    for cluster, cluster_width in zip(clusters, cell_widths):
        if used + cluster_width + ellipsis_width > width:
            break
        kept.append(cluster)
        used += cluster_width
    return ''.join(kept) + ellipsis


def layout_mode(columns, rows):
    if columns < 80 or rows < 18:
        return 'small'
    return 'wide' if columns >= 110 and rows >= 24 else 'compact'


class DraftStore:
    """Keep bounded unsent text and cursor positions by destination."""

    capacity_notice = '50 unsent drafts; send or clear one'

    def __init__(self):
        self._entries = OrderedDict()
        self._revision = 0

    def __len__(self):
        return len(self._entries)

    def items(self):
        return ((key, entry[0]) for key, entry in self._entries.items())

    def get(self, key):
        entry = self._entries.get(key)
        return '' if entry is None else entry[0]

    def get_cursor(self, key):
        entry = self._entries.get(key)
        return 0 if entry is None else entry[1]

    def can_open(self, key, *, mandatory=False):
        return mandatory or key in self._entries or len(self._entries) < MAX_DRAFTS

    def set(self, key, text, *, cursor=None):
        text = str(text)
        if len(text.encode('utf-8', 'surrogatepass')) > MAX_DRAFT_BYTES:
            return False
        if not text:
            self.clear(key)
            return True
        old = self._entries.get(key)
        if old is None and len(self._entries) >= MAX_DRAFTS:
            return False
        position = old[1] if cursor is None and old is not None else (
            len(text) if cursor is None else cursor)
        position = max(0, min(len(text), int(position)))
        if old is None or old[0] != text:
            self._revision += 1
            revision = self._revision
        else:
            revision = old[2]
        self._entries[key] = (text, position, revision)
        return True

    def revision(self, key):
        """Identity of this nonempty text snapshot, or None when absent."""
        entry = self._entries.get(key)
        return entry[2] if entry is not None else None

    def clear(self, key):
        self._entries.pop(key, None)

    def rename(self, old_key, new_key):
        """Move one raw draft and cursor atomically, without consuming capacity."""
        if old_key == new_key:
            return True
        if new_key in self._entries:
            return False
        if old_key not in self._entries:
            return True
        text, cursor, _ = self._entries.pop(old_key)
        self._revision += 1
        self._entries[new_key] = (text, cursor, self._revision)
        return True

    def set_cursor(self, key, position):
        entry = self._entries.get(key)
        if entry is None:
            return
        text, _, revision = entry
        self._entries[key] = (text, max(0, min(len(text), int(position))), revision)


class NoticeStore:
    """Keep newest sanitized activity lines without writing output."""

    def __init__(self):
        self._lines = deque(maxlen=MAX_NOTICE_LINES)
        self.omitted = 0

    def __len__(self):
        return len(self._lines)

    @property
    def lines(self):
        return tuple(self._lines)

    def add(self, text):
        text = body_text(text)
        if not text:
            return
        lines = text.split('\n')
        if lines[-1] == '':
            lines.pop()
        for line in lines:
            if len(self._lines) == self._lines.maxlen:
                self.omitted += 1
            self._lines.append(line)


class Viewport:
    """Track message IDs needed for anchored conversation presentation."""

    def __init__(self, anchor_id=None, follow=True):
        self.anchor_id = anchor_id
        self.line_offset = 0
        self.follow = follow
        self.new_ids = set()
        self._known_ids = ()
        self._initialized = False

    def sync(self, messages, changed_ids=(), deleted_ids=(), reconnect=False):
        current_ids = tuple(messages.keys())[-MAX_VIEWPORT_IDS:]
        current = set(current_ids)
        previous_ids = self._known_ids
        previous = set(previous_ids)

        if not self.follow and self.anchor_id not in current:
            replacement = self._replacement_anchor(previous_ids, current)
            if replacement is None:
                self.follow = True
                self.new_ids.clear()
            else:
                self.anchor_id = replacement
                self.line_offset = 0

        if self.follow:
            self.line_offset = 0
            self.new_ids.clear()
            self.anchor_id = current_ids[-1] if current_ids else None
        elif self._initialized and not reconnect:
            self.new_ids.update(
                message_id for message_id in changed_ids
                if message_id in current and message_id not in previous)
        self.new_ids.intersection_update(current)
        self._known_ids = current_ids
        self._initialized = True

    def _replacement_anchor(self, previous_ids, current):
        if self.anchor_id in previous_ids:
            index = previous_ids.index(self.anchor_id)
            for message_id in previous_ids[index + 1:]:
                if message_id in current:
                    return message_id
            for message_id in reversed(previous_ids[:index]):
                if message_id in current:
                    return message_id
        return None

    def anchor(self, message_id, messages, *, line_offset=0):
        if message_id not in messages:
            return
        self.anchor_id = message_id
        self.line_offset = max(0, int(line_offset))
        self.follow = False

    def mark_seen(self):
        self.follow = True
        self.line_offset = 0
        self.new_ids.clear()
        self.anchor_id = self._known_ids[-1] if self._known_ids else None


@dataclass
class TuiState:
    drafts: DraftStore = field(default_factory=DraftStore)
    notices: NoticeStore = field(default_factory=NoticeStore)
    viewport: Viewport = field(default_factory=Viewport)
    selected_session_id: str | None = None
    selected_agent_id: str | None = None
    search: str = ''
    focus_name: str = 'composer'
