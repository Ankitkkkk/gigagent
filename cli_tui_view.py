"""Persistent, sanitized presentation over the client and controller models."""

from collections import Counter, deque
import sys
from bisect import bisect_right

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer, Float,
                                   FloatContainer, HSplit, VSplit, Window)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, Frame, TextArea

from cli_tui_state import body_text, clip_cells, label_text, layout_mode
from cli_view_contracts import channel_transcript
from cli_workspace_chat import _timestamp
from cli_workspaces import WINDOWS_TMUX_ERROR


def _wrap(text, width):
    """Yield cell-bounded lines, including for unbroken wide Unicode text."""
    width = max(1, width)
    for line in body_text(text).split('\n'):
        part, used = [], 0
        for char in line:
            cells = max(0, get_cwidth(char))
            if used + cells > width and part:
                yield ''.join(part)
                part, used = [], 0
            if cells <= width:
                part.append(char)
                used += cells
        yield ''.join(part)


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


class _ConversationControl(FormattedTextControl):
    def __init__(self, view):
        self.width, self.height = 1, 1
        self.view = view
        super().__init__(self.fragments, focusable=True, show_cursor=False)

    def create_content(self, width, height):
        self.width, self.height = width, height
        return super().create_content(width, height)

    def fragments(self):
        rows = self.view._transcript()
        viewport = self.view.state.viewport
        lines = deque(maxlen=max(1, self.height))
        started = viewport.follow or viewport.anchor_id is None
        for message in rows:
            if message['id'] == viewport.anchor_id:
                started = True
            if not started:
                continue
            content = [f"[{label_text(message.get('time', ''))}] "
                       f"{label_text(message.get('sender', '?'))}:\n{body_text(message.get('text', ''))}"]
            for attachment in message.get('attachments', []):
                name = label_text(attachment.get('name', ''))
                url = label_text(attachment.get('url', ''))
                content.append('Attachment: ' + ' '.join(value for value in (name, url) if value))
            choices = message.get('metadata', {}).get('choices', [])
            if choices:
                content.append('Choices: ' + ' | '.join(label_text(choice) for choice in choices))
            for item in content:
                for line in _wrap(item, self.width):
                    lines.append(line)
                    if not viewport.follow and len(lines) >= self.height:
                        return [('', '\n'.join(lines))]
        return [('', '\n'.join(lines) if lines else 'No messages yet. Write in Message below.')]


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
        self.inspecting = False
        self._inspector_line = 0
        self.composer = TextArea(multiline=True, height=3, wrap_lines=True,
                                 read_only=Condition(lambda: self.screen_mode == 'small'),
                                 input_processors=[_SafeComposer()])
        self.conversation = _ConversationControl(self)
        self.navigation = FormattedTextControl(self._navigation_fragments,
                                               focusable=True, show_cursor=False)
        self.agents = FormattedTextControl(self._agent_fragments,
                                           focusable=True, show_cursor=False)
        self.conversation_window = Window(self.conversation, wrap_lines=False)
        self.navigation_window = Window(self.navigation, wrap_lines=False)
        self.agents_window = Window(self.agents, height=lambda: 8 if self.inspecting else 3, wrap_lines=False)
        conversation = self._frame(self.conversation_window, self._conversation_title, 'conversation')
        agent_area = ConditionalContainer(
            self._frame(self.agents_window, lambda: 'Agent details · Esc Back' if self.inspecting
                        else 'Agents · Enter Inspect · F3 Actions', 'agents'),
            filter=Condition(lambda: not self.controller.plain_channel))
        composer_area = self._frame(self.composer, 'Message', 'composer')
        main = HSplit([conversation, agent_area, composer_area])
        self.new_session = Button('New session', width=20, handler=lambda:
            self._app().create_background_task(self.callbacks['run_action']('new_session'))
            if self.screen_mode == 'wide' and not self.controller.plain_channel else None)
        navigation_area = HSplit([self.navigation_window, ConditionalContainer(
            self.new_session, filter=Condition(lambda: not self.controller.plain_channel))])
        sidebar = self._frame(navigation_area, lambda: 'Channels' if
                              self.controller.plain_channel else 'Sessions', 'navigation',
                              width=Dimension.exact(22))
        wide = VSplit([sidebar, main])
        resize_notice = HSplit([Window(FormattedTextControl(
            'Resize terminal to at least 80 × 18.\nDraft and focus are preserved.'))])
        footer = Window(FormattedTextControl(self._footer), height=1)
        self.key_bindings = self._bindings()
        self.root = FloatContainer(content=HSplit([
            DynamicContainer(lambda: resize_notice if self.screen_mode == 'small'
                             else wide if self.screen_mode == 'wide' else main), footer]),
            floats=[Float(content=DynamicContainer(lambda: self.dialogs.body))],
            key_bindings=self.key_bindings)
        self.refresh()

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
        if name not in ('composer', 'conversation', 'navigation', 'agents', 'new_session'):
            raise ValueError('Unknown focus target: ' + name)
        self.state.focus_name = name
        self._app().layout.focus(getattr(self, name))
        self._app().invalidate()

    def _transcript(self):
        return channel_transcript(self.client.messages, self.client.channel)

    def refresh(self, event=None):
        rows = {message['id']: message for message in self._transcript()}
        changed = event.message_ids if event is not None and event.source == 'client' else ()
        self.state.viewport.sync(rows, changed_ids=changed,
                                 deleted_ids=tuple(ident for ident in changed if ident not in rows),
                                 reconnect=event is not None and event.kind in ('history', 'selection', 'channel'))
        self._app().invalidate()

    def set_sessions(self, rows):
        """Project navigation metadata only; caller owns fetching and warnings."""
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
            fragments.append(('class:selected' if selected else '', clip_cells(label, 20) + '\n'))
            if row.get('archived'):
                fragments.append(('class:muted', '  (archived)\n'))
        if not fragments:
            fragments.append(('', 'No sessions yet\n'))
        return fragments

    def _selected_agent(self):
        return next((agent for agent in (self.controller.workspace or {}).get('agents', [])
                     if agent['agent_id'] == self.state.selected_agent_id), None)

    def inspector_text(self):
        agent = self._selected_agent()
        if agent is None:
            return 'Select an agent to inspect.'
        lines = ['Agent: ' + label_text(agent['agent_id']),
                 label_text(self.controller._agent_status(agent)),
                 'Native session: ' + label_text(agent.get('native_session_id') or 'id unknown')]
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
                        ('history', 'History', 'Show history state'),
                        ('inspect_agent', 'Inspect agent', 'Show full status and recovery details')]
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
            result.append(dict(id=ident, label=label, description=description, disabled_reason=reason))
        return result

    def _agent_fragments(self):
        if self.inspecting:
            width = max(1, self._app().output.get_size().columns -
                        (22 if self.screen_mode == 'wide' else 0) - 2)
            lines = list(_wrap(self.inspector_text(), width))
            self._inspector_line = min(self._inspector_line, max(0, len(lines) - 1))
            return [('', '\n'.join(lines[self._inspector_line:]))]
        agents = (self.controller.workspace or {}).get('agents', [])
        if not agents:
            return [('', 'No agents. Add agent via F4 Commands.')]
        fragments = []
        width = max(1, self._app().output.get_size().columns - (22 if self.screen_mode == 'wide' else 0) - 2)
        for agent in agents:
            selected = agent['agent_id'] == self.state.selected_agent_id
            if selected:
                fragments.append(('[SetCursorPosition]', ''))
            status = agent.get('last_state')
            style = 'class:status.' + status if status in ('running', 'starting', 'failed') else ''
            if selected:
                style = 'class:selected'
            fragments.append((style, clip_cells(label_text(self.controller._agent_status(agent)), width) + '\n'))
        return fragments

    def _conversation_title(self):
        workspace = self.controller.workspace
        name = workspace.get('name') if workspace else '#' + self.client.channel
        title = label_text(name) + ' · ' + label_text(self.client.connection_state).capitalize()
        if self.state.viewport.new_ids:
            title += f' · {len(self.state.viewport.new_ids)} new'
        return clip_cells(title, max(1, self._app().output.get_size().columns - 30))

    def _footer(self):
        if self.screen_mode == 'small':
            return [('class:muted', 'F1 Help · Ctrl+Q Quit')]
        return [('class:muted', 'F2 ' + ('Channels' if self.controller.plain_channel else 'Sessions') +
                 ' · F3 Agents · F4 Commands · F1 Help · Ctrl+Q Quit')]

    def _bindings(self):
        bindings = KeyBindings()

        def dispatch(key, callback, *args):
            @bindings.add(key, filter=Condition(lambda: key in ('f1', 'c-q') or self.screen_mode != 'small'))
            def invoke(event):
                event.app.create_background_task(self.callbacks[callback](*args))

        dispatch('f1', 'run_action', 'help')
        dispatch('f2', 'navigate', False)
        dispatch('f3', 'run_action', 'agents')
        dispatch('f4', 'run_action', 'commands')
        dispatch('c-q', 'quit')

        @bindings.add('c-c')
        def preserve(event):
            pass

        @bindings.add('tab')
        @bindings.add('s-tab')
        def focus(event):
            if self.screen_mode == 'small':
                return
            names = ['navigation', 'new_session', 'conversation', 'agents', 'composer'] if self.screen_mode == 'wide' else [
                'conversation', 'agents', 'composer']
            if self.controller.plain_channel:
                names.remove('agents')
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
                   [agent['agent_id'] for agent in (self.controller.workspace or {}).get('agents', [])])
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
            elif self._selected_agent() is not None:
                self.show_inspector()

        @bindings.add('escape', filter=has_focus(self.agents), eager=True)
        def close_inspector(event):
            self.inspecting = False

        return bindings
