"""Awaitable, terminal-safe dialogs hosted by one running Application."""

import asyncio
from bisect import bisect_right
from dataclasses import dataclass

from prompt_toolkit.filters import has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.widgets import Button, Dialog, Label, RadioList, TextArea

from cli_tui_state import body_text, label_text


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

        @bindings.add('escape', eager=True)
        @bindings.add('c-c', eager=True)
        def cancel(event):
            self.finish(cancel_value)

        bindings.add('tab')(focus_next)
        bindings.add('s-tab')(focus_previous)
        return bindings

    async def _open(self, content, bindings, focus, cancel_value):
        if self.future is not None:
            return cancel_value
        app = self.app_getter()
        future = asyncio.get_running_loop().create_future()
        self.future = future
        self._saved_focus = app.layout.current_control
        self._cancel_value = cancel_value
        self.float = FloatContainer(content=content, floats=[], modal=True,
                                    key_bindings=bindings)
        try:
            app.layout.focus(focus)
            self.invalidate()
            return await future
        finally:
            if self.future is future:
                self.finish(cancel_value)

    def restore_focus(self):
        layout = self.app_getter().layout
        saved, self._saved_focus = self._saved_focus, None
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
        self.restore_focus()
        self.invalidate()
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

        submit_button = Button(label_text(submit_label), handler=submit)
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
