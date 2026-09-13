"""Awaitable, terminal-safe dialogs hosted by one running Application."""

import asyncio
import sys
from bisect import bisect_right
from dataclasses import dataclass, replace
from pathlib import Path

from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.application.current import get_app
from prompt_toolkit.keys import Keys
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.widgets import Button, Dialog, Label, RadioList, TextArea

from cli_api import CLIError
from cli_tui_state import body_text, label_text
from cli_view_contracts import ActionOutcome
from cli_workspace_chat import _agent_cwd, _agent_line, _agent_label, SESSION_HELP
from cli_workspaces import WINDOWS_TMUX_ERROR


@dataclass(frozen=True)
class ModalResult:
    value: object = None
    cancelled: bool = False


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    default: str = ''
    choices: tuple = ()
    required: bool = False


class _SafeInput(Processor):
    """Sanitize displayed input without changing its submitted buffer value."""

    def apply_transformation(self, transformation_input):
        fragments = []
        offsets = [0]
        for style, text, *extra in transformation_input.fragments:
            for character in text:
                visible = body_text(character) if character == '\t' else label_text(character)
                fragments.append((style, visible, *extra))
                offsets.append(offsets[-1] + len(visible))
        return Transformation(
            fragments,
            source_to_display=lambda position: offsets[min(position, len(offsets) - 1)],
            display_to_source=lambda position: max(0, bisect_right(offsets, position) - 1),
        )


class DialogHost:
    """Own one modal waiter and restore focus when that waiter finishes."""

    def __init__(self, app_getter, invalidate):
        self.app_getter = app_getter
        self.invalidate = invalidate
        self.future = None
        self.float = None
        self._empty = Window(height=0, width=0)
        self._saved_focus = None
        self._cancel_value = None

    @property
    def body(self):
        return self.float if self.float is not None else self._empty

    def _bindings(self, cancel_value):
        bindings = KeyBindings()

        # A queued continuation belongs to Alt editing. Let native multi-key
        # bindings consume it; only a lone Escape bypasses timeoutlen.
        @bindings.add('escape', eager=Condition(lambda: not get_app().key_processor.input_queue))
        @bindings.add('c-c', eager=True)
        def cancel(event):
            self.finish(cancel_value)

        @bindings.add('escape', Keys.Any)
        def unknown_alt(event):
            # Specific native Alt bindings outrank this wildcard fallback.
            # Unknown Alt sequences must not cancel and leak into the composer.
            pass

        bindings.add('tab')(focus_next)
        bindings.add('s-tab')(focus_previous)
        return bindings

    async def _open(self, content, bindings, focus, cancel_value):
        if self.future is not None:
            return cancel_value
        app = self.app_getter()
        if app is None or not app.is_running:
            return cancel_value
        future = asyncio.get_running_loop().create_future()
        self.future = future
        self._saved_focus = app.layout.current_control
        self._cancel_value = cancel_value
        self.float = FloatContainer(content=content, floats=[], modal=True,
                                    key_bindings=bindings)
        try:
            app.layout.update_parents_relations()
            app.layout.focus(focus)
            self.invalidate()
            return await future
        finally:
            if self.future is future:
                self.finish(cancel_value)

    def restore_focus(self):
        saved, self._saved_focus = self._saved_focus, None
        app = self.app_getter()
        if app is None or not app.is_running:
            return
        layout = app.layout
        controls = [container.content for container in walk(layout.container, skip_hidden=True)
                    if isinstance(container, Window)]
        if saved in controls and saved.is_focusable():
            layout.focus(saved)
        else:
            for control in controls:
                if control.is_focusable():
                    layout.focus(control)
                    break

    def finish(self, value):
        future, self.future = self.future, None
        if future is None:
            return
        self.float = None
        self._cancel_value = None
        try:
            self.restore_focus()
            self.invalidate()
        finally:
            if not future.done():
                future.set_result(value)

    def cancel(self):
        """Cancel any open dialog, including during application shutdown."""
        self.finish(self._cancel_value)

    async def confirm(self, text, *, default=False, escape=False):
        """Return bool; escape MUST mean non-mutation (No/Chat only/stay).

        A busy second confirmation returns its escape answer immediately.
        """
        bindings = self._bindings(escape)

        @bindings.add('y')
        @bindings.add('Y')
        def yes(event):
            self.finish(True)

        @bindings.add('n')
        @bindings.add('N')
        def no(event):
            self.finish(False)

        yes_button = Button('Yes', handler=lambda: self.finish(True))
        no_button = Button('No', handler=lambda: self.finish(False))
        dialog = Dialog(title='Confirm', body=Label(body_text(text)),
                        buttons=[yes_button, no_button], modal=False)
        return await self._open(dialog, bindings,
                                yes_button if default else no_button, escape)

    async def form(self, title, fields, *, submit_label, error=None):
        """Collect raw field values; show validation errors until resubmitted."""
        cancelled = ModalResult(cancelled=True)
        if self.future is not None:
            return cancelled
        fields = tuple(fields)
        controls = {}
        rows = []
        for field in fields:
            if field.choices:
                control = RadioList([(value, label_text(value)) for value in field.choices],
                                    default=field.default, select_on_focus=True)
            else:
                control = TextArea(text=field.default, multiline=False, height=1,
                                   input_processors=[_SafeInput()])
            controls[field.name] = control
            rows.extend([Label(label_text(field.label)), control])
        error_label = Label(body_text(error) if error is not None else '')
        rows.append(error_label)

        def submit():
            values = {name: (control.current_value if isinstance(control, RadioList)
                             else control.text) for name, control in controls.items()}
            for field in fields:
                if field.required and not values[field.name].strip():
                    error_label.text = label_text(field.label).rstrip(':') + ' is required'
                    self.app_getter().layout.focus(controls[field.name])
                    self.invalidate()
                    return
            self.finish(ModalResult(values))

        submit_button = Button(label_text(submit_label),
                               width=max(12, get_cwidth(label_text(submit_label)) + 4), handler=submit)
        cancel_button = Button('Cancel', handler=self.cancel)
        bindings = self._bindings(cancelled)

        @bindings.add('enter', filter=~has_focus(cancel_button), eager=True)
        def accept(event):
            submit()

        dialog = Dialog(title=label_text(title), body=HSplit(rows),
                        buttons=[submit_button, cancel_button], modal=False)
        first = next(iter(controls.values()), submit_button)
        return await self._open(dialog, bindings, first, cancelled)

    async def choose(self, title, choices, *, searchable=True):
        """Choose mapping ID; descriptions and disabled reasons stay visible."""
        cancelled = ModalResult(cancelled=True)
        if self.future is not None:
            return cancelled
        choices = tuple(choices)
        visible = list(choices)
        selected_id = visible[0]['id'] if visible else None
        search = TextArea(multiline=False, height=1, prompt='Search: ',
                          input_processors=[_SafeInput()])

        def render_rows():
            fragments = []
            for choice in visible:
                selected = choice['id'] == selected_id
                if selected:
                    fragments.append(('[SetCursorPosition]', ''))
                style = 'class:dialog.choice.selected' if selected else ''
                text = ('> ' if selected else '  ') + label_text(choice['label'])
                if choice.get('description'):
                    text += ' — ' + label_text(choice['description'])
                if choice.get('disabled_reason'):
                    text += ' (' + label_text(choice['disabled_reason']) + ')'
                fragments.append((style, text + '\n'))
            return fragments or [('', 'No matching choices')]

        def refilter(buffer):
            nonlocal visible, selected_id
            query = label_text(buffer.text).casefold()
            visible = [choice for choice in choices if query in ' '.join(
                label_text(choice.get(key, '')) for key in ('id', 'label', 'description')
            ).casefold()]
            if selected_id not in [choice['id'] for choice in visible]:
                selected_id = visible[0]['id'] if visible else None
            self.invalidate()

        search.buffer.on_text_changed += refilter
        rows = Window(FormattedTextControl(render_rows, focusable=True),
                      height=Dimension(min=1, max=10), wrap_lines=True)

        def submit():
            choice = next((choice for choice in visible if choice['id'] == selected_id), None)
            if choice is not None and not choice.get('disabled_reason'):
                self.finish(ModalResult(choice['id']))

        select_button = Button('Select', handler=submit)
        cancel_button = Button('Cancel', handler=self.cancel)
        bindings = self._bindings(cancelled)

        @bindings.add('up', eager=True)
        @bindings.add('down', eager=True)
        def move(event):
            nonlocal selected_id
            if visible:
                ids = [choice['id'] for choice in visible]
                index = ids.index(selected_id)
                offset = -1 if event.key_sequence[-1].key == 'up' else 1
                selected_id = ids[max(0, min(len(ids) - 1, index + offset))]

        @bindings.add('enter', filter=~has_focus(cancel_button), eager=True)
        def accept(event):
            submit()

        content = HSplit([search, rows] if searchable else [rows])
        dialog = Dialog(title=label_text(title), body=content,
                        buttons=[select_button, cancel_button], modal=False)
        return await self._open(dialog, bindings, search if searchable else rows, cancelled)


class TuiWorkflows:
    """Gather user intent, then call typed controller/client operations once."""

    def __init__(self, client, controller, view, dialogs, state, *, composer_actions):
        self.client, self.controller, self.view = client, controller, view
        self.dialogs, self.state = dialogs, state
        self.composer_actions = composer_actions
        self.include_archived = False
        self._active = set()
        self._list_request = 0

    def notice(self, text):
        self.state.notices.add(text)
        self.dialogs.invalidate()

    def _scope(self):
        return ((self.controller.workspace or {}).get('id'), self.controller.selection_generation)

    def _unchanged(self, scope):
        return scope == self._scope()

    def _cancelled_selection(self):
        message = 'Selection changed; action cancelled'
        self.notice(message)
        return ActionOutcome('cancelled', message)

    def _admit(self, key, mandatory=False):
        if self.state.drafts.can_open(key, mandatory=mandatory):
            return True
        self.notice(self.state.drafts.capacity_notice)
        return False

    async def confirm_selection(self, text, *, workspace, default=False, escape=False):
        if self.dialogs.future is not None:
            return escape
        self.view.hide_help()
        # The controller supplies its actual candidate, including unarchive changes.
        lines = ['Session: ' + label_text(workspace.get('name') or workspace['id']),
                 label_text(workspace['id'])]
        for agent in workspace.get('agents', []):
            if isinstance(agent, dict) and agent.get('last_state') == 'exited':
                prefix = 'Eligible: ' if _agent_cwd(agent) is not None else 'Skipped: '
                lines.append(prefix + _agent_line(agent))
        lines.extend(['', text])
        return await self.dialogs.confirm('\n'.join(lines), default=default, escape=escape)

    async def refresh_sessions(self):
        """Refresh navigation metadata; errors retain old rows and publish once."""
        self._list_request += 1
        request = self._list_request
        self.view.set_sessions_loading(True)
        try:
            response = await self.controller.list_sessions(include_archived=self.include_archived)
            if request != self._list_request:
                return ActionOutcome('cancelled')
            self.view.set_sessions(response['workspaces'])
            if response.get('warning'):
                self.notice(response['warning'])
            return ActionOutcome('completed')
        except (CLIError, OSError, TimeoutError) as error:
            if request != self._list_request:
                return ActionOutcome('cancelled')
            message = str(error) if isinstance(error, CLIError) else 'Session list unavailable; retry Refresh.'
            self.view.set_sessions_error(message)
            return ActionOutcome('failed', message)
        finally:
            if request == self._list_request:
                self.view.set_sessions_loading(False)

    async def _navigation(self):
        """Specialized modal, sharing DialogHost cancellation and focus semantics."""
        cancelled = ModalResult(cancelled=True)
        if self.dialogs.future is not None:
            return cancelled
        search = TextArea(text=self.state.search, multiline=False, height=1, prompt='Search: ',
                          input_processors=[_SafeInput()])
        tasks = set()
        loading = False

        def visible():
            query = search.text.casefold()
            return [row for row in self.view.session_rows() if query in
                    (str(row.get('name') or '') + ' ' + row['id']).casefold()]

        def refilter(buffer):
            self.state.search = buffer.text
            ids = [row['id'] for row in visible()]
            if self.state.selected_session_id not in ids:
                self.state.selected_session_id = next(iter(ids), None)
            self.dialogs.invalidate()
        search.buffer.on_text_changed += refilter

        def choose(ident):
            if not loading:
                self.state.selected_session_id = ident
                self.dialogs.finish(ModalResult(ident))

        def fragments():
            rows = []
            for row in visible():
                ident = row['id']
                if ident == self.state.selected_session_id:
                    rows.append(('[SetCursorPosition]', ''))
                def mouse(event, ident=ident):
                    if event.event_type == MouseEventType.MOUSE_UP:
                        choose(ident)
                text = ('> ' if ident == self.state.selected_session_id else '  ')
                text += label_text(row.get('name') or ident) + ' [' + label_text(ident) + ']'
                if row.get('archived'):
                    text += ' (archived)'
                rows.append(('class:dialog.choice.selected' if ident == self.state.selected_session_id else '',
                             text + '\n', mouse))
            return rows or [('', 'No matching sessions')]

        rows = Window(FormattedTextControl(fragments, focusable=True),
                      height=Dimension(min=1, max=10), wrap_lines=True)
        status = Label(lambda: 'Loading sessions…' if loading else
                       'Session list stale; retry Refresh.' if self.view.sessions_stale else '')

        async def fetch():
            nonlocal loading
            loading = True
            self.dialogs.invalidate()
            try:
                await self.refresh_sessions()
                refilter(search.buffer)
            finally:
                loading = False
                self.dialogs.invalidate()

        def refresh():
            if loading or tasks:
                return
            task = asyncio.create_task(fetch())
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        def toggle():
            if not loading and not tasks:
                self.include_archived = not self.include_archived
                refresh()

        new = Button('New session', width=15, handler=lambda: self.dialogs.finish(ModalResult('__new__')))
        archived = Button('Show archived', width=17, handler=toggle)
        refresh_button = Button('Refresh', handler=refresh)
        cancel = Button('Cancel', handler=self.dialogs.cancel)
        bindings = self.dialogs._bindings(cancelled)

        @bindings.add('up', eager=True)
        @bindings.add('down', eager=True)
        def move(event):
            ids = [row['id'] for row in visible()]
            if ids:
                selected = self.state.selected_session_id
                index = ids.index(selected) if selected in ids else 0
                offset = -1 if event.key_sequence[-1].key == 'up' else 1
                self.state.selected_session_id = ids[max(0, min(len(ids) - 1, index + offset))]

        @bindings.add('enter', filter=has_focus(search) | has_focus(rows), eager=True)
        def select(event):
            if self.state.selected_session_id in [row['id'] for row in visible()]:
                choose(self.state.selected_session_id)

        dialog = Dialog(title='Sessions', body=HSplit([search, status, rows]),
                        buttons=[new, archived, refresh_button, cancel], modal=False)
        refresh()
        try:
            return await self.dialogs._open(dialog, bindings, search, cancelled)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def navigate(self, mandatory=False):
        if self.controller.plain_channel:
            return await self.run_action('switch_channel')
        if self.dialogs.future is not None:
            return ActionOutcome('cancelled')
        result = await self._navigation()
        if result.cancelled:
            if self.controller.workspace is None:
                await self.view.callbacks['quit']()
            return ActionOutcome('cancelled')
        if result.value == '__new__':
            return await self.new_session(mandatory=mandatory)
        return await self._select(result.value, mandatory=mandatory)

    async def _select(self, ident, *, mandatory=False):
        if not self._admit(('session', ident), mandatory):
            return ActionOutcome('cancelled')
        scope = self._scope()
        outcome = await self.controller.select_session(ident)
        if outcome.status == 'completed':
            if (self.controller.workspace or {}).get('id') != ident:
                return self._cancelled_selection()
            self.state.selected_session_id = ident
            self.composer_actions.switch_draft(self.composer_actions.destination_key(), mandatory=True)
            if not self._unchanged(scope):
                self.state.selected_agent_id = None
                self.state.viewport.mark_seen()
            await self.refresh_sessions()
        return outcome

    async def new_session(self, *, mandatory=False):
        # A fresh server ID cannot already own a draft.
        if not self._admit(('new_session', None), mandatory):
            return ActionOutcome('cancelled')
        scope = self._scope()
        fields = [Field('name', 'Session name:')]
        error = None
        while True:
            result = await self.dialogs.form('New session', fields, submit_label='Create session', error=error)
            if result.cancelled:
                return ActionOutcome('cancelled')
            if not self._unchanged(scope):
                return self._cancelled_selection()
            if not self._admit(('new_session', None), mandatory):
                return ActionOutcome('cancelled')
            outcome = await self.controller.execute_action('create_session', result.value)
            if outcome.status != 'failed':
                break
            error = outcome.message
            fields = [replace(f, default=result.value[f.name]) for f in fields]
        if outcome.status == 'completed':
            await self.refresh_sessions()
            return await self._select(outcome.workspace_id, mandatory=mandatory)
        return outcome

    async def _choose_agent(self, agent_id):
        rows = self.view.agent_rows()
        if agent_id is not None:
            return agent_id if any(a['agent_id'] == agent_id for a in rows) else None
        result = await self.dialogs.choose('Choose agent', [dict(id=a['agent_id'],
            label=_agent_label(a), description=_agent_line(a)) for a in rows])
        return None if result.cancelled else result.value

    async def agent_form(self, action, agent_id=None, *, _scope=None):
        scope = self._scope() if _scope is None else _scope
        if self.controller.workspace is None:
            return self._failure('Select a session first')
        if action in ('spawn', 'resume') and sys.platform == 'win32':
            return self._failure(WINDOWS_TMUX_ERROR)
        if action != 'spawn':
            agent_id = await self._choose_agent(agent_id)
            if not self._unchanged(scope):
                return self._cancelled_selection()
            if agent_id is None:
                return ActionOutcome('cancelled')
        agent = next((a for a in self.view.agent_rows() if a['agent_id'] == agent_id), {})
        if action == 'spawn':
            last_cwd = next((a['cwd'] for a in reversed(self.view.agent_rows()) if a.get('cwd')), None)
            fields = [Field('provider', 'Provider:', choices=tuple(self.controller.providers), required=True),
                      Field('cwd', 'Working directory:', default=last_cwd or str(Path.cwd()), required=True),
                      Field('name', 'Agent name:'),
                      Field('history_mode', 'History mode [none/literal]:', default='literal',
                            choices=('none', 'literal'), required=True)]
            title, submit = 'New agent', 'Start agent'
        elif action == 'resume':
            fields = [Field('cwd', 'Working directory (blank keeps stored):'),
                      Field('name', 'Agent name (blank keeps stored):'),
                      Field('launch_mode', 'Launch mode:', default='ordinary', choices=('ordinary', 'fresh'))]
            title, submit = 'Resume agent', 'Resume agent'
        elif action == 'history':
            fields = [Field('mode', 'History mode:', default=agent.get('history_mode', 'literal'),
                            choices=('literal', 'none'), required=True)]
            title, submit = 'History settings', 'Apply history mode'
        else:
            return self._failure('Unsupported agent form')
        error = None
        while True:
            context = ''
            if action != 'spawn':
                current = next((a for a in self.view.agent_rows() if a['agent_id'] == agent_id), agent)
                context = '\n'.join([_agent_line(current),
                    'History: ' + str(current.get('history_state') or 'unknown'),
                    str(current.get('history_note') or '')])
            result = await self.dialogs.form(title, fields, submit_label=submit,
                                             error='\n'.join(filter(None, [context, error])) or None)
            if result.cancelled:
                return ActionOutcome('cancelled')
            if not self._unchanged(scope):
                return self._cancelled_selection()
            values = result.value
            fields = [replace(f, default=values[f.name]) for f in fields]
            cwd = values.get('cwd')
            if cwd and (not Path(cwd).is_absolute() or not Path(cwd).is_dir()):
                error = 'Working directory must be an absolute existing directory.'
                continue
            if action == 'spawn':
                payload = dict(values, name=values['name'] or None)
            elif action == 'resume':
                fresh = values['launch_mode'] == 'fresh'
                if fresh:
                    accepted = await self.dialogs.confirm(
                        'Fresh launch for ' + _agent_label(agent) + '? [y/N]', default=False, escape=False)
                    if not self._unchanged(scope):
                        return self._cancelled_selection()
                    if not accepted:
                        continue
                payload = dict(agent_id=agent_id, fresh=fresh, cwd=cwd or None, name=values['name'] or None)
            else:
                payload = dict(agent_id=agent_id, mode=values['mode'])
            outcome = await self.controller.execute_action(action, payload)
            if outcome.status != 'failed':
                return outcome
            error = outcome.message

    def _failure(self, message):
        self.notice(message)
        return ActionOutcome('failed', message)

    async def show_palette(self):
        scope = self._scope()
        agent_id = self.state.selected_agent_id
        result = await self.dialogs.choose('Commands', self.view.action_choices())
        if result.cancelled:
            return ActionOutcome('cancelled')
        if not self._unchanged(scope):
            return self._cancelled_selection()
        return await self._dispatch(result.value, agent_id, scope)

    async def run_action(self, action_id, *, target_id=None):
        scope = self._scope()
        agent_actions = {'resume', 'stop', 'attach', 'unread', 'retry', 'history', 'inspect_agent'}
        if target_id is None and action_id in agent_actions:
            target_id = self.state.selected_agent_id
        return await self._dispatch(action_id, target_id, scope)

    async def _dispatch(self, action_id, target_id, scope):
        key = (scope, action_id, target_id)
        if key in self._active:
            return ActionOutcome('cancelled')
        self._active.add(key)
        try:
            return await self._run(action_id, target_id, scope)
        finally:
            self._active.discard(key)

    async def _run(self, action, target_id, scope):
        if action == 'commands':
            return await self.show_palette()
        if action == 'quit':
            await self.view.callbacks['quit']()
        elif action == 'new_session':
            return await self.new_session()
        elif action == 'switch_session':
            return await self.view.callbacks['navigate']()
        elif action == 'select_session':
            return await self._select(target_id) if target_id else ActionOutcome('cancelled')
        elif action == 'refresh':
            return await self.refresh_sessions()
        elif action == 'activity':
            self.view.show_activity()
        elif action == 'clear_draft':
            return ActionOutcome('completed' if await self.composer_actions.clear_draft() else 'cancelled')
        elif action == 'help':
            if self.view.help_visible:
                self.view.hide_help()
                return ActionOutcome('completed')
            from cli import HELP
            text = ('F2 Sessions/Channels · F3 Agents · F4 Commands · F5 Activity\n'
                    'Tab changes focus · Enter selects/sends · Alt+Enter adds a line\n'
                    'Ctrl+Q Quit · Escape cancels · Ctrl+C preserves draft\n'
                    'Sessions opens navigation. Committed switches and Quit checkpoint.\n\n' + HELP + '\n' +
                    SESSION_HELP.replace('/sessions            Checkpoint, then choose a session',
                                         '/sessions            Open session navigation'))
            self.view.show_help(text)
        elif action in ('switch_channel', 'create_channel'):
            return await self._channel(action)
        elif action == 'history' and self.controller.plain_channel:
            result = await self.client.submit_outcome('/history')
            self.view.hide_activity()
            self.state.viewport.mark_seen()
            self.view.focus_named('conversation')
            return ActionOutcome(result.status, result.message)
        elif action in ('new_agent', 'resume', 'history'):
            return await self.agent_form('spawn' if action == 'new_agent' else action, target_id, _scope=scope)
        elif action in ('rename_session', 'archive_session'):
            if scope[0] is None:
                return self._failure('Select a session first')
            name = self.controller.workspace.get('name') or scope[0]
            if action == 'rename_session':
                fields, error = [Field('name', 'Session name:', default=name)], None
                while True:
                    result = await self.dialogs.form('Rename session', fields, submit_label='Rename', error=error)
                    if result.cancelled:
                        return ActionOutcome('cancelled')
                    if not self._unchanged(scope):
                        return self._cancelled_selection()
                    outcome = await self.controller.execute_action(action, result.value)
                    if outcome.status != 'failed':
                        break
                    error = outcome.message
                    fields = [replace(f, default=result.value[f.name]) for f in fields]
            else:
                accepted = await self.dialogs.confirm(label_text(name) + '\nArchive session? [y/N]',
                                                       default=False, escape=False)
                if not self._unchanged(scope):
                    return self._cancelled_selection()
                if not accepted:
                    return ActionOutcome('cancelled')
                outcome = await self.controller.execute_action(action, {'confirmed': True})
                if outcome.status == 'completed':
                    self.composer_actions.switch_draft(None, mandatory=True)
                    await self.refresh_sessions()
                    return await self.view.callbacks['navigate'](mandatory=True)
            if outcome.status == 'completed':
                await self.refresh_sessions()
            return outcome
        elif action in ('agents', 'inspect_agent', 'stop', 'attach', 'unread', 'retry'):
            if self.controller.plain_channel and action == 'agents':
                result = await self.client.submit_outcome('/agents')
                self.view.show_activity()
                return ActionOutcome(result.status, result.message)
            ident = await self._choose_agent(target_id)
            if not self._unchanged(scope):
                return self._cancelled_selection()
            if ident is None:
                return ActionOutcome('cancelled')
            if action in ('agents', 'inspect_agent'):
                self.state.selected_agent_id = ident
                self.view.show_inspector()
            else:
                if action in ('attach', 'stop') and sys.platform == 'win32':
                    return self._failure(WINDOWS_TMUX_ERROR)
                if action == 'stop':
                    agent = next(a for a in self.view.agent_rows() if a['agent_id'] == ident)
                    accepted = await self.dialogs.confirm('Stop ' + _agent_label(agent) + '? [y/N]',
                                                          default=False, escape=False)
                    if not self._unchanged(scope):
                        return self._cancelled_selection()
                    if not accepted:
                        return ActionOutcome('cancelled')
                outcome = await self.controller.execute_action(action, {'agent_id': ident})
                if action in ('unread', 'retry'):
                    self.view.show_activity()
                return outcome
        else:
            return self._failure('Unknown action: ' + str(action))
        return ActionOutcome('completed')

    async def _channel(self, action):
        if not self.controller.plain_channel:
            return self._failure('Channel navigation requires plain-channel mode')
        original = self.client.channel
        fields, error = [Field('name', 'Channel name:', required=True)], None
        while True:
            if action == 'switch_channel':
                result = await self.dialogs.choose('Channels', [dict(id=name, label=name)
                                                               for name in self.client.channels])
                name = result.value
            else:
                result = await self.dialogs.form('Create channel', fields, submit_label='Create channel', error=error)
                name = result.value['name'] if not result.cancelled else None
            if result.cancelled:
                return ActionOutcome('cancelled')
            if original != self.client.channel:
                return self._cancelled_selection()
            if not self._admit(('channel', name)):
                return ActionOutcome('cancelled')
            outcome = await self.client.submit_outcome(('/join ' if action == 'switch_channel' else '/create ') + name)
            if outcome.status != 'failed' or action == 'switch_channel':
                if outcome.status == 'completed' and self.client.channel != original:
                    self.composer_actions.switch_draft(self.composer_actions.destination_key())
                    self.state.viewport.mark_seen()
                return ActionOutcome(outcome.status, outcome.message)
            error = outcome.message
            fields = [replace(f, default=name) for f in fields]
