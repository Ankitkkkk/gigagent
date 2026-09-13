"""Persistent, sanitized presentation over the client and controller models."""

from collections import Counter, deque
import sys
import re
from bisect import bisect_right

from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer, Float,
                                   FloatContainer, HSplit, VSplit, Window)
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, Frame, TextArea

from cli_tui_state import MAX_DRAFT_BYTES, body_text, clip_cells, label_text, layout_mode
from cli_view_contracts import channel_transcript
from cli_workspace_chat import SESSION_COMMANDS, SESSION_HELP, _timestamp
from cli_workspaces import WINDOWS_TMUX_ERROR


def _wrap(text, width):
    """Wrap by terminal cells, keeping words intact whenever they fit."""
    width = max(1, width)
    for line in body_text(text).split('\n'):
        part, used = '', 0
        for match in re.finditer(r'\s+|\S+', line):
            token = match.group()
            cells = get_cwidth(token)
            if cells <= width:
                if used + cells > width:
                    yield part.rstrip()
                    part, used = '', 0
                    if token.isspace():
                        continue
                part += token
                used += cells
                continue
            if part:
                yield part.rstrip()
                part, used = '', 0
            for char in token:
                cells = max(0, get_cwidth(char))
                if used + cells > width and part:
                    yield part
                    part, used = '', 0
                if cells <= width:
                    part += char
                    used += cells
        yield part


class _SafeComposer(Processor):
    """Sanitize displayed buffer text while preserving raw edits and offsets."""

    def apply_transformation(self, ti):
        fragments, offsets = [], [0]
        for style, text, *extra in ti.fragments:
            for character in text:
                visible = body_text(character) if character == '\t' else label_text(character)
                fragments.append((style, visible, *extra))
                offsets.append(offsets[-1] + len(visible))
        return Transformation(fragments,
            source_to_display=lambda position: offsets[min(position, len(offsets) - 1)],
            display_to_source=lambda position: max(0, bisect_right(offsets, position) - 1))


class _ConversationControl(UIControl):
    """Use actual render dimensions, never FormattedTextControl sizing caches."""

    def __init__(self, view):
        self.width, self.height = 1, 1
        self.view = view
        self._visible_start = (None, 0)

    def is_focusable(self):
        return True

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            self.view.focus_named('conversation')
            self.view.state.viewport.mark_seen()
            self.view._app().invalidate()
            return None
        return NotImplemented

    def page(self, direction):
        ids = self.view._transcript_ids()
        if not ids:
            return
        ident, offset = self._visible_start
        index = ids.index(ident) if ident in ids else 0
        remaining = max(1, self.height)

        def count(position):
            message = self.view._message(ids[position])
            return sum(1 for _ in self._message_lines(message, self.width)) if message else 1
        if direction < 0:
            while remaining > offset and index > 0:
                remaining -= offset
                index -= 1
                offset = count(index)
            offset = max(0, offset - remaining)
        else:
            offset += remaining
            length = count(index)
            while offset >= length and index + 1 < len(ids):
                offset -= length
                index += 1
                length = count(index)
            offset = min(offset, max(0, length - 1))
        self.view.state.viewport.anchor(ids[index], self.view.client.messages, line_offset=offset)

    def create_content(self, width, height):
        self.width, self.height = width, height
        lines = self._lines(width, height)
        return UIContent(get_line=lambda index: [('', lines[index])],
                         line_count=len(lines), show_cursor=False)

    def text(self):
        """Sanitized source fragments, also useful to renderer-level tests."""
        return [('', '\n'.join(self._lines(self.width, self.height)))]

    def _message_lines(self, message, width):
        yield from _wrap(f"[{label_text(message.get('time', ''))}] "
                         f"{label_text(message.get('sender', '?'))}:", width)
        yield from _wrap(message.get('text', ''), width)
        for attachment in message.get('attachments', []):
            name = label_text(attachment.get('name', ''))
            url = label_text(attachment.get('url', ''))
            yield from _wrap('Attachment: ' + ' '.join(value for value in (name, url) if value), width)
        choices = message.get('metadata', {}).get('choices', [])
        if choices:
            yield from _wrap('Choices: ' + ' | '.join(label_text(choice) for choice in choices), width)

    def _lines(self, width, height):
        height = max(1, height)
        ids = self.view._transcript_ids()
        viewport = self.view.state.viewport
        lines = deque()
        if viewport.follow:
            for ident in reversed(ids):
                message = self.view._message(ident)
                if message is None:
                    continue
                tail = deque(enumerate(self._message_lines(message, width)), maxlen=height - len(lines))
                if tail:
                    self._visible_start = (ident, tail[0][0])
                lines.extendleft(line for _, line in reversed(tail))
                if len(lines) >= height:
                    break
        else:
            start = ids.index(viewport.anchor_id) if viewport.anchor_id in ids else 0
            for ident in ids[start:]:
                message = self.view._message(ident)
                if message is None:
                    continue
                offset = viewport.line_offset if ident == viewport.anchor_id else 0
                message_lines = self._message_lines(message, width)
                # Clamp a saved offset if a message edit shortened its body.
                tail = deque(maxlen=1)
                visible = False
                for line_index, line in enumerate(message_lines):
                    tail.append((line_index, line))
                    if line_index < offset:
                        continue
                    if not lines:
                        self._visible_start = (ident, line_index)
                    visible = True
                    lines.append(line)
                    if len(lines) >= height:
                        return list(lines)
                if not visible and tail:
                    line_index, line = tail[0]
                    self._visible_start = (ident, line_index)
                    viewport.line_offset = line_index
                    lines.append(line)
        return list(lines) or ['No messages yet. Write in Message below.']


class _ActivityControl(UIControl):
    def __init__(self, view):
        self.view = view
        self.height = 1

    def is_focusable(self):
        return True

    def create_content(self, width, height):
        self.height = max(1, height)
        lines = [line for notice in self.view.state.notices.lines for line in _wrap(notice, width)]
        lines = lines or ['No activity yet. Esc returns to conversation.']
        self.view._activity_max_line = max(0, len(lines) - self.height)
        self.view._activity_line = min(self.view._activity_line, self.view._activity_max_line)
        visible = lines[self.view._activity_line:self.view._activity_line + self.height]
        return UIContent(get_line=lambda index: [('', visible[index])],
                         line_count=len(visible), show_cursor=False)


class _HelpControl(UIControl):
    def __init__(self, view):
        self.view = view
        self.height = 1
        self.last_line = 0

    def is_focusable(self):
        return True

    def create_content(self, width, height):
        self.height = max(1, height)
        lines = list(_wrap(self.view._help_text, width))
        self.last_line = max(0, len(lines) - self.height)
        self.view._help_line = min(self.view._help_line, self.last_line)
        visible = lines[self.view._help_line:self.view._help_line + self.height]
        return UIContent(get_line=lambda index: [('', visible[index])],
                         line_count=len(visible), show_cursor=False)


class ContextualCompleter(Completer):
    """Resolve suggestions from current authoritative models on every request."""

    @staticmethod
    def _help_descriptions(help_text):
        descriptions = {}
        for line in help_text.splitlines():
            if line.startswith('/'):
                command = line.split()[0]
                columns = re.split(r'\s{2,}', line, maxsplit=1)
                descriptions[command] = (columns[1] if len(columns) == 2 else
                                         line.partition(' ')[2]) or 'Chat command'
        return descriptions

    def _commands(self):
        # Lazy import keeps cli's future lazy TUI entry path free of an import cycle.
        from cli import HELP
        commands = {command: 'Server chat command' for command in
                    re.findall(r'/[a-z][a-z_-]*', HELP)}
        commands.update(self._help_descriptions(HELP))
        if not self.view.controller.plain_channel:
            commands.pop('/join', None)
            commands.pop('/create', None)
            descriptions = self._help_descriptions(SESSION_HELP)
            applicable = SESSION_COMMANDS if self.view.controller.workspace is not None else {'/sessions'}
            commands.update({command: descriptions.get(command, 'Session command')
                             for command in sorted(applicable)})
        return commands

    def __init__(self, view):
        self.view = view

    def get_completions(self, document, complete_event):
        client, controller = self.view.client, self.view.controller
        before = document.text_before_cursor
        word = re.search(r'\S*$', before).group()
        prior = before[:-len(word)] if word else before
        parts = prior.split()
        commands = self._commands()
        agents = (controller.workspace or {}).get('agents', [])
        handles = []
        for agent in agents:
            if not isinstance(agent, dict) or not isinstance(agent.get('agent_id'), str) or not agent['agent_id']:
                continue
            handle = agent.get('registry_name') or agent['agent_id']
            if isinstance(handle, str):
                handles.append(handle)
        candidates = {}
        if not parts and word.startswith('/'):
            candidates = commands
        elif word.startswith('@'):
            candidates = {'@' + name: 'Mention agent' for name in
                          dict.fromkeys([*client.agent_names, *handles])}
        elif parts and parts[0] in commands:
            command = parts[0]
            if controller.plain_channel and command == '/join' and len(parts) == 1:
                candidates = {(('#' if word.startswith('#') else '') + name): 'Channel'
                              for name in client.channels}
            elif not controller.plain_channel and controller.workspace is not None:
                if command == '/spawn' and len(parts) == 1:
                    candidates = {name: 'Configured provider' for name in controller.providers}
                elif command in ('/resume', '/stop', '/attach', '/retry', '/unread', '/history') and len(parts) == 1:
                    candidates = {name: 'Session agent' for name in handles}
                elif (command == '/history' and len(parts) == 2 or
                      command == '/spawn' and parts[-1] == '--history-mode'):
                    candidates = {'literal': 'Literal history', 'none': 'No history'}
        elif word.startswith('#'):
            candidates = {'#' + name: 'Channel' for name in client.channels}
        for value, description in candidates.items():
            if value.startswith(word):
                yield Completion(value, start_position=-len(word),
                                 display=[('', label_text(value))],
                                 display_meta=[('', label_text(description))])


class ComposerActions:
    """Bind one persistent buffer to bounded destination drafts and submission."""

    def __init__(self, view, state, submit, notice):
        self.view, self.state, self.submit, self.notice = view, state, submit, notice
        self.key = None
        self.edit_version = 0
        self.sending = False
        self._restoring = False
        self._last_rejection = None
        self.buffer = view.composer.buffer
        self._accepted = Document()
        self.buffer.on_text_changed += self._edited
        self.buffer.on_cursor_position_changed += self._cursor_changed
        previous_read_only = self.buffer.read_only
        self.buffer.read_only = Condition(lambda: self.key is None or previous_read_only())
        self.buffer.completer = ContextualCompleter(view)
        self.buffer.complete_while_typing = Condition(lambda: self.key is not None)
        self._bindings()
        self.switch_draft(self.destination_key(), mandatory=True)

    def destination_key(self):
        """Return authoritative selection identity; None disables the composer."""
        if self.view.controller.plain_channel:
            return ('channel', self.view.client.channel)
        workspace = self.view.controller.workspace
        return ('session', workspace['id']) if workspace is not None else None

    def rename_channel(self, old_name, new_name):
        old_key, new_key = ('channel', old_name), ('channel', new_name)
        if not self.state.drafts.rename(old_key, new_key):
            self.notice('Channel draft collision; both drafts were kept')
            return False
        if self.key == old_key:
            self.key = new_key
            self.edit_version += 1
            self._last_rejection = None
        elif self.key == new_key:
            # Settings may have already selected the renamed destination while empty.
            self._restore(Document(self.state.drafts.get(new_key), self.state.drafts.get_cursor(new_key)))
            self.edit_version += 1
            self._last_rejection = None
        self.view._app().invalidate()
        return True

    def _restore(self, document):
        self._restoring = True
        try:
            self.buffer.set_document(document, bypass_readonly=True)
            self._accepted = document
        finally:
            self._restoring = False

    def _reject_edit(self, message):
        if message != self._last_rejection:
            self._last_rejection = message
            self.notice(message)

    def _edited(self, buffer):
        if self._restoring:
            return
        document = buffer.document
        if self.key is None or not self.state.drafts.set(
                self.key, document.text, cursor=document.cursor_position):
            too_large = len(document.text.encode('utf-8', 'surrogatepass')) > MAX_DRAFT_BYTES
            self._restore(self._accepted)
            self._reject_edit('Select a destination before editing' if self.key is None else
                              'Draft exceeds 64 KiB UTF-8; edit rejected' if too_large else
                              self.state.drafts.capacity_notice)
            return
        self._last_rejection = None
        self.edit_version += 1
        self._accepted = document

    def _cursor_changed(self, buffer):
        if not self._restoring:
            self._accepted = buffer.document
            if self.key is not None:
                self.state.drafts.set_cursor(self.key, buffer.cursor_position)

    def switch_draft(self, key, *, mandatory=False):
        """Bind admitted destination text/cursor; caller owns selection commits."""
        if key == self.key:
            return True
        full = key is not None and not self.state.drafts.can_open(key)
        if full and not mandatory:
            self.notice(self.state.drafts.capacity_notice)
            return False
        if full:
            self.notice(self.state.drafts.capacity_notice)
        self.key = key
        self._last_rejection = self.state.drafts.capacity_notice if full else None
        self.edit_version += 1
        self._restore(Document(self.state.drafts.get(key), self.state.drafts.get_cursor(key))
                      if key is not None else Document())
        self.view._app().invalidate()
        return True

    def _claim_send(self):
        """Capture the Enter-time snapshot before queued typeahead can edit it."""
        if self.sending or self.key is None or not self.buffer.text.strip():
            return
        if self.key != self.destination_key():
            self.notice('Draft destination changed; select its destination before sending')
            return
        self.sending = True
        return self.key, self.buffer.text, self.state.drafts.revision(self.key)

    async def send(self):
        snapshot = self._claim_send()
        if snapshot is not None:
            await self._send_snapshot(snapshot)

    async def _send_snapshot(self, snapshot):
        key, text, revision = snapshot
        try:
            # Selection may have changed after the synchronous key handler returned.
            if key != self.destination_key():
                self.notice('Draft destination changed; select its destination before sending')
                return
            outcome = await self.submit(text)
            if outcome.status == 'completed':
                if revision == self.state.drafts.revision(key):
                    self.state.drafts.clear(key)
                    if key == self.key:
                        self.edit_version += 1
                        self._restore(Document())
                elif outcome.sent:
                    self.notice('The earlier message was sent; your current draft was kept')
            if not outcome.keep_running:
                await self.view.callbacks['quit']()
        finally:
            self.sending = False
            self.view._app().invalidate()

    async def clear_draft(self):
        key, version = self.key, self.edit_version
        if key is None or not self.buffer.text:
            return False
        if not await self.view.dialogs.confirm('Clear draft? [y/N]', default=False, escape=False):
            return False
        if key != self.key or version != self.edit_version:
            return False
        self.state.drafts.clear(key)
        self.edit_version += 1
        self._restore(Document())
        return True

    def _bindings(self):
        bindings = self.view.key_bindings
        focused = has_focus(self.view.composer) & Condition(lambda:
            self.view.screen_mode != 'small' and self.view.dialogs.future is None)
        editable = focused & Condition(lambda: self.key is not None)

        @bindings.add('enter', filter=focused)
        def send(event):
            completion = self.buffer.complete_state
            if completion is not None and completion.complete_index is not None:
                self.buffer.apply_completion(completion.current_completion)
                return
            self.buffer.complete_state = None
            snapshot = self._claim_send()
            if snapshot is not None:
                started = False
                async def submit_snapshot():
                    nonlocal started
                    started = True
                    await self._send_snapshot(snapshot)
                task = event.app.create_background_task(submit_snapshot())
                def release_unstarted(done):
                    if not started:
                        self.sending = False
                        event.app.invalidate()
                task.add_done_callback(release_unstarted)

        @bindings.add('escape', 'enter', filter=editable)
        def newline(event):
            self.buffer.insert_text('\n')

        @bindings.add('tab', filter=editable & Condition(lambda: self.buffer.complete_state is not None))
        def complete(event):
            self.buffer.complete_next()

        @bindings.add('c-c', filter=focused)
        def cancel(event):
            if self.buffer.complete_state is not None:
                # Keep the visible candidate, rather than restoring pre-completion text.
                self.buffer.complete_state = None
            else:
                self.view._cancel_overlay()

        @bindings.add('escape', filter=focused,
                      eager=Condition(lambda: self.buffer.complete_state is not None and
                                      not self.view._app().key_processor.input_queue))
        def escape(event):
            if self.buffer.complete_state is not None:
                self.buffer.complete_state = None

        @bindings.add('c-d', filter=focused)
        def delete_or_quit(event):
            if not self.buffer.text:
                event.app.create_background_task(self.view.callbacks['quit']())
            elif self.key is not None:
                self.buffer.delete()


class TuiView:
    """Build controls once. Callbacks own application actions and all I/O."""

    style = Style.from_dict({
        'frame.border': 'ansibrightblack', 'frame.label': '',
        'focused frame.border': 'ansicyan', 'selected': 'ansicyan bold',
        'status.running': 'ansigreen', 'status.starting': 'ansiyellow',
        'status.failed': 'ansired', 'muted': 'ansibrightblack',
        'dialog.choice.selected': 'ansicyan bold',
        'button': 'bg:default fg:default', 'button.focused': 'bg:default ansicyan bold',
    })

    def __init__(self, client, controller, state, dialogs, callbacks):
        self.client, self.controller = client, controller
        self.state, self.dialogs, self.callbacks = state, dialogs, callbacks
        self._sessions = []
        self._sessions_loading = False
        self._sessions_stale = False
        self._transcript_key = None
        self._ordered_ids = ()
        self.inspecting = False
        self._inspector_line = 0
        self.activity_visible = False
        self.help_visible = False
        self._help_text = ''
        self._help_line = 0
        self._help_focus = None
        self.help = _HelpControl(self)
        self._activity_line = 0
        self._activity_max_line = 0
        self._activity_focus = None
        self.composer = TextArea(multiline=True, height=self._composer_height, wrap_lines=True,
                                 read_only=Condition(lambda: self.screen_mode == 'small'),
                                 input_processors=[_SafeComposer()])
        self.conversation = _ConversationControl(self)
        self.activity = _ActivityControl(self)
        self.navigation = FormattedTextControl(self._navigation_fragments,
                                               focusable=True, show_cursor=False)
        self.agents = FormattedTextControl(self._agent_fragments,
                                           focusable=True, show_cursor=False)
        self.conversation_window = Window(self.conversation, wrap_lines=False)
        self.navigation_window = Window(self.navigation, wrap_lines=False)
        self.agents_window = Window(self.agents, height=lambda: 8 if self.inspecting else
                                    1 if self.screen_mode == 'compact' else 3, wrap_lines=False)
        conversation = self._frame(self.conversation_window, self._conversation_title, 'conversation')
        self.agent_actions = Button('Actions', width=12, handler=lambda:
            self._app().create_background_task(self.callbacks['run_action'](
                'agents', target_id=self.state.selected_agent_id)))
        agent_contents = VSplit([self.agents_window, self.agent_actions], padding=1)
        framed_agents = self._frame(agent_contents, lambda: 'Agent details · Esc Back' if self.inspecting
                                    else 'Agents · Enter Actions · F3 Actions', 'agents')
        agent_area = ConditionalContainer(DynamicContainer(lambda: agent_contents
            if self.screen_mode == 'compact' and not self.inspecting else framed_agents),
            filter=Condition(lambda: not self.controller.plain_channel))
        self.clear_draft = Button('Clear draft', width=15, handler=lambda:
            self._app().create_background_task(self.callbacks['run_action']('clear_draft')))
        composer_area = FloatContainer(
            content=self._frame(self.composer, self._composer_title, 'composer'),
            floats=[Float(top=0, right=1, content=self.clear_draft)])
        activity_area = self._frame(Window(self.activity), lambda:
            f'Activity · {self.state.notices.omitted} omitted · Esc Back', 'activity')
        main = HSplit([DynamicContainer(lambda: activity_area if self.activity_visible else conversation),
                       agent_area, composer_area])
        self.new_session = Button('New session', width=20, handler=lambda:
            self._app().create_background_task(self.callbacks['run_action']('new_session'))
            if self.screen_mode == 'wide' and not self.controller.plain_channel else None)
        navigation_area = HSplit([self.navigation_window, ConditionalContainer(
            self.new_session, filter=Condition(lambda: not self.controller.plain_channel))])
        sidebar = self._frame(navigation_area, lambda: 'Channels' if
                              self.controller.plain_channel else 'Sessions · Stale' if self._sessions_stale
                              else 'Sessions', 'navigation',
                              width=Dimension.exact(22))
        wide = VSplit([sidebar, main])
        resize_notice = HSplit([Window(FormattedTextControl(
            'Resize terminal to at least 80 × 18.\nDraft and focus are preserved.'))])
        footer = Window(FormattedTextControl(self._footer), height=1)
        notice_strip = ConditionalContainer(Window(FormattedTextControl(self._notice_text), height=1),
            filter=Condition(lambda: self.screen_mode != 'small' and bool(self.state.notices.lines)))
        self.global_key_bindings = KeyBindings()
        self.key_bindings = self._bindings()
        self.root = FloatContainer(content=HSplit([
            DynamicContainer(lambda: resize_notice if self.screen_mode == 'small'
                             else wide if self.screen_mode == 'wide' else main), notice_strip, footer]),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=6,
                        extra_filter=has_focus(self.composer) & Condition(lambda:
                            self.screen_mode != 'small' and self.dialogs.future is None))),
                    Float(content=DynamicContainer(lambda: self.dialogs.body)),
                    Float(content=ConditionalContainer(self._help_container(),
                        filter=Condition(lambda: self.help_visible)))],
            key_bindings=self.key_bindings)
        self.refresh()

    def _help_container(self):
        bindings = KeyBindings()

        @bindings.add('escape', eager=Condition(lambda: not self._app().key_processor.input_queue))
        @bindings.add('c-c', eager=True)
        def close(event):
            self.hide_help()

        @bindings.add('escape', Keys.Any)
        @bindings.add('tab')
        @bindings.add('s-tab')
        def preserve(event):
            pass

        @bindings.add('up')
        @bindings.add('down')
        @bindings.add('pageup')
        @bindings.add('pagedown')
        @bindings.add('home')
        @bindings.add('end')
        def scroll(event):
            key = event.key_sequence[-1].key
            if key == 'home':
                self._help_line = 0
            elif key == 'end':
                self._help_line = self.help.last_line
            else:
                amount = self.help.height if key in ('pageup', 'pagedown') else 1
                self._help_line = max(0, self._help_line + (-amount if key in ('up', 'pageup') else amount))
        content = Frame(Window(self.help,
            height=lambda: max(3, min(20, self._app().output.get_size().rows - 4)),
            width=lambda: max(10, min(90, self._app().output.get_size().columns - 4))),
            title='Help · F1/Esc Back · PgUp/PgDn Scroll')
        return FloatContainer(content=content, floats=[], modal=True, key_bindings=bindings)

    def show_help(self, text):
        if self.help_visible:
            return False
        self._help_focus = self._app().layout.current_window
        self._help_text = body_text(text)
        self._help_line = 0
        self.help_visible = True
        self._app().layout.update_parents_relations()
        self._app().layout.focus(self.help)
        self._app().invalidate()
        return True

    def hide_help(self):
        if not self.help_visible:
            return False
        saved, self._help_focus = self._help_focus, None
        self.help_visible = False
        visible = [node.content for node in walk(self.root, skip_hidden=True) if isinstance(node, Window)]
        if self.dialogs.future is not None:
            modal = [node for node in walk(self.dialogs.body, skip_hidden=True)
                     if isinstance(node, Window) and node.content.is_focusable()]
            current = self._app().layout.current_window
            if saved in modal:
                self._app().layout.current_window = saved
            elif current not in modal and modal:
                self._app().layout.current_window = modal[0]
        elif saved is not None and (saved.content in visible or self.screen_mode == 'small'):
            # A resize can hide the saved pane; retain its actual focus window.
            self._app().layout.current_window = saved
        else:
            self._app().layout.current_window = self.composer.window
        self._app().invalidate()
        return True

    def _composer_title(self):
        key = (('channel', self.client.channel) if self.controller.plain_channel else
               ('session', self.controller.workspace['id']) if self.controller.workspace else None)
        if key is not None and not self.state.drafts.can_open(key):
            return 'Message · ' + self.state.drafts.capacity_notice
        return 'Message'

    def _composer_height(self):
        width = max(1, self._app().output.get_size().columns -
                    (22 if self.screen_mode == 'wide' else 0) - 2)
        lines = sum(max(1, (get_cwidth(line) + width - 1) // width)
                    for line in body_text(self.composer.text).split('\n'))
        return min(6, max(3, lines))

    def _app(self):
        try:
            return self.dialogs.app_getter()
        except AttributeError:  # Initial construction precedes Application composition.
            return get_app()

    def _frame(self, body, title, name, width=None):
        return HSplit([Frame(body, title=title)], width=width,
                      style=self._focus_style(name))

    @property
    def screen_mode(self):
        size = self._app().output.get_size()
        return layout_mode(size.columns, size.rows)

    def _focus_style(self, name):
        def style():
            target = getattr(self, name)
            control = getattr(target, 'control', target)
            return 'class:focused' if self._app().layout.current_control is control else ''
        return style

    def focus_named(self, name):
        if name not in ('composer', 'conversation', 'navigation', 'agents', 'new_session', 'activity', 'agent_actions', 'clear_draft'):
            raise ValueError('Unknown focus target: ' + name)
        target = getattr(self, name)
        control = getattr(target, 'control', target)
        visible = [node.content for node in walk(self.root, skip_hidden=True) if isinstance(node, Window)]
        if control not in visible:
            return False
        self._app().layout.focus(target)
        self.state.focus_name = name
        self._app().invalidate()
        return True

    def _transcript_ids(self):
        key = (self.client.view_revision, self.client.channel)
        if key != self._transcript_key:
            self._ordered_ids = tuple(message['id'] for message in
                channel_transcript(self.client.messages, self.client.channel))
            self._transcript_key = key
        return self._ordered_ids

    def _message(self, ident):
        message = self.client.messages.get(ident)
        if message is None or message.get('channel', 'general') != self.client.channel:
            self._transcript_key = None
            return None
        return message

    def _transcript(self):
        for ident in self._transcript_ids():
            message = self._message(ident)
            if message is not None:
                yield message

    def refresh(self, event=None):
        rows = {message['id']: message for message in self._transcript()}
        changed = event.message_ids if event is not None and event.source == 'client' else ()
        self.state.viewport.sync(rows, changed_ids=changed,
                                 deleted_ids=tuple(ident for ident in changed if ident not in rows),
                                 reconnect=event is not None and event.kind in ('history', 'selection', 'channel'))
        self._app().invalidate()

    def session_rows(self):
        return tuple(self._sessions)

    @property
    def sessions_stale(self):
        return self._sessions_stale

    def set_sessions(self, rows):
        """Project navigation metadata only; caller owns fetching and warnings."""
        self._sessions_stale = False
        self._sessions = [{key: row.get(key) for key in ('id', 'name', 'archived', 'updated_at')}
                          for row in rows]
        self._sessions.sort(key=lambda row: _timestamp(row.get('updated_at')), reverse=True)
        ids = [row['id'] for row in self._sessions]
        if self.state.selected_session_id not in ids:
            current = (self.controller.workspace or {}).get('id')
            self.state.selected_session_id = current if current in ids else next(iter(ids), None)
        self._app().invalidate()

    def set_sessions_loading(self, loading):
        self._sessions_loading = bool(loading)
        self._app().invalidate()

    def set_sessions_error(self, text):
        """Retain old rows; own the stale marker and one diagnostic notice."""
        self._sessions_loading = False
        self._sessions_stale = True
        self.state.notices.add(text)
        self._app().invalidate()

    def _notice_text(self):
        latest = self.state.notices.lines[-1] if self.state.notices.lines else ''
        return [('class:muted', clip_cells('Notice: ' + body_text(latest),
                                         self._app().output.get_size().columns))]

    def show_activity(self):
        """Open the read-only NoticeStore surface, retaining prior chat focus."""
        if self.activity_visible or self.screen_mode == 'small' or self.dialogs.future is not None:
            return False
        current = self._app().layout.current_control
        name = next((name for name in ('composer', 'conversation', 'navigation', 'agents', 'new_session')
                     if current is getattr(getattr(self, name), 'control', getattr(self, name))),
                    self.state.focus_name)
        self._activity_focus = (current, name)
        self._activity_line = 0
        self.activity_visible = True
        return self.focus_named('activity')

    def hide_activity(self):
        """Keep current visible focus; otherwise restore prior focus or composer."""
        if not self.activity_visible or self.screen_mode == 'small':
            return False
        current = self._app().layout.current_control
        self.activity_visible = False
        saved, name = self._activity_focus
        self._activity_focus = None
        visible = [node.content for node in walk(self.root, skip_hidden=True) if isinstance(node, Window)]
        if current is not self.activity and current in visible:
            self._app().invalidate()
        elif saved in visible:
            self._app().layout.focus(saved)
            self.state.focus_name = name
            self._app().invalidate()
        else:
            self.focus_named('composer')
        return True

    def _navigation_fragments(self):
        if self.controller.plain_channel:
            return [('', clip_cells(('> ' if channel == self.client.channel else '  ') +
                                    label_text(channel), 20) + '\n') for channel in self.client.channels]
        workspace = self.controller.workspace or {}
        current = workspace.get('id')
        rows = [dict(row, name=workspace.get('name'), archived=workspace.get('archived'))
                if row['id'] == current else row for row in self._sessions]
        counts = Counter(label_text(row.get('name') or row['id']) for row in rows)
        fragments = [('class:muted', 'Loading sessions…\n')] if self._sessions_loading else []
        for row in rows:
            ident = row['id']
            name = label_text(row.get('name') or ident)
            suffix = (' [' + label_text(ident)[-6:] + ']') if counts[name] > 1 else ''
            selected = ident == self.state.selected_session_id
            if selected:
                fragments.append(('[SetCursorPosition]', ''))
            prefix = '*' if ident == current else '>' if selected else ' '
            # Reserve cells for disambiguation/archive suffixes before clipping names.
            label = prefix + ' ' + clip_cells(name, max(0, 18 - get_cwidth(suffix))) + suffix
            def activate(event, ident=ident):
                if event.event_type == MouseEventType.MOUSE_UP and self.dialogs.future is None:
                    self._app().create_background_task(self.callbacks['run_action'](
                        'select_session', target_id=ident))
            fragments.append(('class:selected' if selected else '', clip_cells(label, 20) + '\n', activate))
            if row.get('archived'):
                fragments.append(('class:muted', '  (archived)\n'))
        if not fragments:
            fragments.append(('', 'No sessions yet\n'))
        return fragments

    def agent_rows(self):
        """Transient validated references into the authoritative workspace."""
        return [agent for agent in (self.controller.workspace or {}).get('agents', [])
                if isinstance(agent, dict) and isinstance(agent.get('agent_id'), str)
                and agent['agent_id']]

    def _selected_agent(self):
        return next((agent for agent in self.agent_rows()
                     if agent['agent_id'] == self.state.selected_agent_id), None)

    def inspector_text(self):
        agent = self._selected_agent()
        if agent is None:
            return 'Select an agent to inspect.'
        lines = ['Agent: ' + label_text(agent['agent_id']),
                 label_text(self.controller._agent_status(agent)),
                 'Native session: ' + ('id present' if agent.get('native_session_id') else 'id unknown')]
        for key, caption in [('cwd', 'Working directory'), ('history_note', 'History'),
                             ('last_error', 'Error')]:
            if agent.get(key):
                lines.append(caption + ': ' + body_text(agent[key]))
        return '\n'.join(lines)

    def show_inspector(self):
        self.inspecting = True
        self._inspector_line = 0
        self.focus_named('agents')

    def action_choices(self):
        """Stable UI action IDs with current, readable disabled reasons."""
        workspace = self.controller.workspace
        agent = self._selected_agent()
        choices = [('help', 'Help', 'Keyboard and command help'),
                   ('activity', 'Activity', 'Read diagnostics; F5 opens Activity, Esc returns'),
                   ('clear_draft', 'Clear draft', 'Clear the current unsent message')]
        if self.controller.plain_channel:
            choices += [('create_channel', 'Create channel', 'Create a chat channel'),
                        ('switch_channel', 'Switch channel', 'Choose a chat channel'),
                        ('history', 'History', 'Show channel history')]
        else:
            choices += [('new_session', 'New session', 'Create a session'),
                        ('switch_session', 'Switch session', 'Choose another session'),
                        ('rename_session', 'Rename session', 'Rename the active session'),
                        ('archive_session', 'Archive session', 'Archive the active session'),
                        ('refresh', 'Refresh', 'Fetch the latest session list'),
                        ('new_agent', 'Add agent', 'Start an agent in this session'),
                        ('attach', 'Attach', 'Open the selected agent terminal'),
                        ('resume', 'Resume agent', 'Resume the selected stopped agent'),
                        ('stop', 'Stop agent', 'Stop the selected agent'),
                        ('unread', 'Unread', 'Inspect unread messages'),
                        ('retry', 'Retry unread', 'Retry delivery of unread messages'),
                        ('history', 'History settings', 'Inspect and change selected agent history mode'),
                        ('inspect_agent', 'Inspect agent', 'Show full status and recovery details')]
        choices.append(('quit', 'Quit', 'Disconnect' if self.controller.plain_channel else 'Checkpoint and disconnect'))
        agent_actions = {'attach', 'resume', 'stop', 'unread', 'retry', 'inspect_agent'}
        session_actions = agent_actions | {'rename_session', 'archive_session', 'new_agent', 'history'}
        result = []
        for ident, label, description in choices:
            reason = ''
            if not self.controller.plain_channel:
                if ident in session_actions and workspace is None:
                    reason = 'Select a session first'
                elif ident in agent_actions and agent is None:
                    reason = 'Select an agent first'
                elif ident == 'attach' and not agent.get('tmux_session'):
                    reason = 'Agent has no terminal; resume it first'
                elif ident == 'stop' and agent.get('last_state') not in ('running', 'starting'):
                    reason = 'Agent is already stopped'
                elif ident == 'resume' and agent.get('last_state') in ('running', 'starting'):
                    reason = 'Agent is already running'
                if ident in {'new_agent', 'attach', 'resume', 'stop'} and sys.platform == 'win32':
                    reason = WINDOWS_TMUX_ERROR
            if self.controller.selection_pending and ident not in {'help', 'activity', 'quit'}:
                reason = 'Session selection in progress'
            result.append(dict(id=ident, label=label, description=description, disabled_reason=reason))
        return result

    def _agent_fragments(self):
        if self.inspecting:
            width = max(1, self._app().output.get_size().columns -
                        (22 if self.screen_mode == 'wide' else 0) - 15)
            lines = list(_wrap(self.inspector_text(), width))
            self._inspector_line = min(self._inspector_line, max(0, len(lines) - 1))
            return [('', '\n'.join(lines[self._inspector_line:]))]
        agents = self.agent_rows()
        if not agents:
            return [('', 'No agents. Actions → Add agent.')]
        fragments = []
        width = max(1, self._app().output.get_size().columns - (22 if self.screen_mode == 'wide' else 0) - 15)
        for agent in agents:
            selected = agent['agent_id'] == self.state.selected_agent_id
            if selected:
                fragments.append(('[SetCursorPosition]', ''))
            status = agent.get('last_state')
            style = 'class:status.' + status if status in ('running', 'starting', 'failed') else ''
            if selected:
                style = 'class:selected'
            def select(event, ident=agent['agent_id']):
                if event.event_type == MouseEventType.MOUSE_UP and self.dialogs.future is None:
                    self.state.selected_agent_id = ident
                    self.focus_named('agents')
            fragments.append((style, clip_cells(label_text(self.controller._agent_status(agent)), width) + '\n', select))
        return fragments

    def _conversation_title(self):
        workspace = self.controller.workspace
        name = (workspace.get('name') or workspace['id']) if workspace else '#' + self.client.channel
        title = label_text(name) + ' · ' + label_text(self.client.connection_state).capitalize()
        if self.state.viewport.new_ids:
            title += f' · {len(self.state.viewport.new_ids)} new'
        return clip_cells(title, max(1, self._app().output.get_size().columns - 30))

    def _footer(self):
        if self.screen_mode == 'small':
            return [('class:muted', 'F1 Help · Ctrl+Q Quit')]
        return [('class:muted', 'F2 ' + ('Channels' if self.controller.plain_channel else 'Sessions') +
                 ' · F3 Agents · F4 Commands · F5 Activity · F1 Help · Ctrl+Q Quit')]

    def _cancel_overlay(self):
        if self.screen_mode == 'small':
            return
        if self.activity_visible:
            self.hide_activity()
        elif self.inspecting:
            self.inspecting = False
            self._app().invalidate()

    def _bindings(self):
        bindings = KeyBindings()

        def dispatch(key, callback, *args):
            @self.global_key_bindings.add(key, filter=Condition(lambda:
                (key in ('f1', 'c-q') or self.screen_mode != 'small') and
                (key in ('f1', 'c-q') or self.dialogs.future is None and not self.help_visible)))
            def invoke(event):
                event.app.create_background_task(self.callbacks[callback](*args))

        dispatch('f1', 'run_action', 'help')
        dispatch('f2', 'navigate', False)
        @self.global_key_bindings.add('f3', filter=Condition(lambda:
            self.screen_mode != 'small' and self.dialogs.future is None and not self.help_visible))
        def agent_actions(event):
            event.app.create_background_task(self.callbacks['run_action'](
                'agents', target_id=self.state.selected_agent_id))
        dispatch('f4', 'run_action', 'commands')
        dispatch('c-q', 'quit')

        @self.global_key_bindings.add('f5', filter=Condition(lambda:
            self.screen_mode != 'small' and self.dialogs.future is None and not self.help_visible))
        def activity(event):
            self.hide_activity() if self.activity_visible else self.show_activity()

        @bindings.add('c-c')
        def preserve(event):
            self._cancel_overlay()

        @bindings.add('tab')
        @bindings.add('s-tab')
        def focus(event):
            if self.screen_mode == 'small':
                return
            names = ['navigation', 'new_session', 'conversation', 'agents', 'agent_actions', 'composer', 'clear_draft'] if self.screen_mode == 'wide' else [
                'conversation', 'agents', 'agent_actions', 'composer', 'clear_draft']
            if self.activity_visible:
                names[names.index('conversation')] = 'activity'
            if self.controller.plain_channel:
                names.remove('agents')
                names.remove('agent_actions')
                if 'new_session' in names:
                    names.remove('new_session')
            current = next((name for name in names if event.app.layout.current_control is
                            getattr(getattr(self, name), 'control', getattr(self, name))),
                           self.state.focus_name)
            offset = -1 if event.key_sequence[-1].key == 's-tab' else 1
            self.focus_named(names[(names.index(current) + offset) % len(names)] if current in names else 'composer')

        @bindings.add('up', filter=has_focus(self.navigation) | has_focus(self.agents))
        @bindings.add('down', filter=has_focus(self.navigation) | has_focus(self.agents))
        def move(event):
            if self.screen_mode == 'small':
                return
            offset = -1 if event.key_sequence[-1].key == 'up' else 1
            if event.app.layout.current_control is self.agents and self.inspecting:
                self._inspector_line = max(0, self._inspector_line + offset)
                return
            navigation = event.app.layout.current_control is self.navigation
            if navigation and self.screen_mode != 'wide':
                return
            if navigation and self.controller.plain_channel:
                return  # Channel selection belongs to the channel navigation workflow.
            ids = ([row['id'] for row in self._sessions] if navigation else
                   [agent['agent_id'] for agent in self.agent_rows()])
            attribute = 'selected_session_id' if navigation else 'selected_agent_id'
            selected = getattr(self.state, attribute)
            if ids:
                index = ids.index(selected) if selected in ids else (-1 if offset > 0 else len(ids))
                setattr(self.state, attribute, ids[max(0, min(len(ids) - 1, index + offset))])

        @bindings.add('enter', filter=has_focus(self.navigation) | has_focus(self.agents))
        def activate(event):
            if self.screen_mode == 'small':
                return
            if event.app.layout.current_control is self.navigation:
                if self.screen_mode != 'wide':
                    return
                if self.controller.plain_channel:
                    event.app.create_background_task(self.callbacks['run_action']('switch_channel'))
                elif self.state.selected_session_id is not None:
                    event.app.create_background_task(self.callbacks['run_action'](
                        'select_session', target_id=self.state.selected_session_id))
                else:
                    event.app.create_background_task(self.callbacks['run_action']('new_session'))
            else:
                event.app.create_background_task(self.callbacks['run_action'](
                    'agents', target_id=self.state.selected_agent_id))

        @bindings.add('escape', filter=has_focus(self.agents),
                      eager=Condition(lambda: not self._app().key_processor.input_queue))
        def close_inspector(event):
            self.inspecting = False

        @bindings.add('pageup', filter=has_focus(self.conversation))
        @bindings.add('pagedown', filter=has_focus(self.conversation))
        @bindings.add('end', filter=has_focus(self.conversation))
        def scroll_conversation(event):
            if self.screen_mode == 'small':
                return
            key = event.key_sequence[-1].key
            if key == 'end':
                self.state.viewport.mark_seen()
            else:
                self.conversation.page(-1 if key == 'pageup' else 1)

        @bindings.add('up', filter=has_focus(self.activity))
        @bindings.add('down', filter=has_focus(self.activity))
        @bindings.add('pageup', filter=has_focus(self.activity))
        @bindings.add('pagedown', filter=has_focus(self.activity))
        @bindings.add('home', filter=has_focus(self.activity))
        @bindings.add('end', filter=has_focus(self.activity))
        def scroll_activity(event):
            key = event.key_sequence[-1].key
            if key == 'home':
                self._activity_line = 0
            elif key == 'end':
                self._activity_line = self._activity_max_line
            else:
                offset = self.activity.height if key in ('pageup', 'pagedown') else 1
                if key in ('up', 'pageup'):
                    offset = -offset
                self._activity_line = max(0, min(self._activity_max_line, self._activity_line + offset))

        @bindings.add('escape', filter=has_focus(self.activity),
                      eager=Condition(lambda: not self._app().key_processor.input_queue))
        def close_activity(event):
            self.hide_activity()

        @bindings.add('escape', Keys.Any, filter=has_focus(self.agents) | has_focus(self.activity))
        def consume_alt(event):
            pass

        return bindings
