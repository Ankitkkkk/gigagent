"""Pipe-input Application harness; capture actual renderer cells, never ANSI parsing."""

import asyncio
from contextlib import asynccontextmanager
import io

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.widgets import Button
from prompt_toolkit.output.vt100 import Vt100_Output

from cli import ChatClient
from cli_tui_dialogs import DialogHost
from cli_tui_state import TuiState
from cli_tui_view import ComposerActions, TuiView
from cli_view_contracts import ActionOutcome, SubmitOutcome
from cli_workspace_chat import WorkspaceChatController


class _HarnessAPI:
    def list(self, *, include_archived=False):
        return {'workspaces': []}


class TuiHarness:
    """Bounded render waits and controlled callbacks; no network or provider startup."""

    sequences = {'F1': '\x1bOP', 'F2': '\x1bOQ', 'F3': '\x1bOR', 'F4': '\x1bOS', 'F5': '\x1b[15~',
                 'Enter': '\r', 'Escape': '\x1b', 'CtrlQ': '\x11', 'CtrlC': '\x03',
                 'CtrlD': '\x04', 'AltEnter': '\x1b\r', 'Left': '\x1b[D', 'Right': '\x1b[C', 'CtrlSpace': '\x00', 'Tab': '\t', 'ShiftTab': '\x1b[Z', 'Home': '\x1b[H', 'End': '\x1b[F',
                 'PageUp': '\x1b[5~', 'PageDown': '\x1b[6~', 'Up': '\x1b[A', 'Down': '\x1b[B'}

    def __init__(self, size, client, controller, pipe):
        self.size = Size(rows=size[1], columns=size[0])
        self.stream = io.StringIO()
        self.pipe = pipe
        self.calls = []
        self.rendered = asyncio.Event()
        self.render_count = 0
        self.rows = []
        self.cells = {}
        self.client = client or (controller.client if controller else
                                 ChatClient('http://127.0.0.1:18300'))
        if controller is not None and controller.client is not self.client:
            raise ValueError('controller must own the supplied client')
        self.controller = controller or WorkspaceChatController(self.client, _HarnessAPI())
        self.api = self.controller.api
        self.state = TuiState()
        self.dialogs = DialogHost(lambda: self.application, self.invalidate,
            owner_is_short_lived=lambda owner: owner in getattr(self, 'tasks', ())
            or owner in self.application._background_tasks)

        async def submit(text):
            self.calls.append(('submit', text))
            return SubmitOutcome('cancelled')

        async def quit():
            self.calls.append(('quit',))

        async def navigate(mandatory=False):
            self.calls.append(('navigate', mandatory))
            return ActionOutcome('cancelled')

        async def run_action(action_id, *, target_id=None):
            self.calls.append(('run_action', action_id, target_id))
            return ActionOutcome('cancelled')

        async def attach(agent_id):
            self.calls.append(('attach', agent_id))
            return ActionOutcome('cancelled', agent_id=agent_id)

        self.callbacks = dict(submit=submit, quit=quit, navigate=navigate,
                              run_action=run_action, attach=attach)
        self.view = TuiView(self.client, self.controller, self.state,
                            self.dialogs, self.callbacks)
        self.application = Application(
            layout=Layout(self.view.root, focused_element=self.view.composer),
            input=pipe, output=Vt100_Output(self.stream, get_size=lambda: self.size,
                                           enable_cpr=False),
            full_screen=True, mouse_support=True, key_bindings=self.view.global_key_bindings,
            style=self.view.style, after_render=self._capture)
        self.key_count = 0
        self.input_processed = asyncio.Event()
        self.application.key_processor.after_key_press += self._key_processed
        self.application.ttimeoutlen = 0.05
        self.application.timeoutlen = 1.0
        self._old = (self.client.on_view_change, self.client.output,
                     self.client.on_workspace, self.client.on_settings,
                     self.controller.presentation, self.controller.on_view_change)
        harness = self

        class Presenter:
            confirm = self.dialogs.confirm

            async def attach(self, agent):
                return await attach(agent['agent_id'])

            def notice(self, text):
                harness.notice(text)

        self.client.on_view_change = self.view.refresh
        self.client.output = self.notice
        self.controller.bind_view(Presenter(), self.view.refresh)

    def _key_processed(self, sender):
        self.key_count += 1
        self.input_processed.set()

    def invalidate(self):
        if hasattr(self, 'application'):
            self.application.invalidate()

    def notice(self, text):
        self.state.notices.add(text)
        self.invalidate()

    def _capture(self, application):
        screen = application.renderer.last_rendered_screen
        if screen is None:
            return
        self.cells = {(x, y): screen.data_buffer[y][x].char
                      for y in range(self.size.rows) for x in range(self.size.columns)}
        self.rows = [''.join(self.cells[x, y] for x in range(self.size.columns)).rstrip()
                     for y in range(self.size.rows)]
        self.render_count += 1
        self.rendered.set()

    def cell(self, x, y):
        return self.cells[x, y]

    def screen_text(self):
        return '\n'.join(self.rows)

    @property
    def focused_control(self):
        current = self.application.layout.current_control
        for name in ('composer', 'conversation', 'navigation', 'agents', 'new_session', 'activity', 'agent_actions', 'clear_draft'):
            target = getattr(self.view, name)
            if current is getattr(target, 'control', target):
                return name
        return 'dialog'

    async def wait_until(self, predicate, timeout=2):
        async def wait():
            while not predicate():
                self.rendered.clear()
                self.application.invalidate()
                await self.rendered.wait()
        await asyncio.wait_for(wait(), timeout)

    async def wait_render(self, timeout=2):
        count = self.render_count
        await self.wait_until(lambda: self.render_count > count, timeout)

    async def key(self, name):
        """Escape includes the configured 50ms terminal escape decoding timeout."""
        await self._send(self.sequences[name])

    def bind_submit(self, submit):
        self.submit_mock = submit
        self.callbacks['submit'] = submit
        if not hasattr(self, 'composer_actions'):
            self.composer_actions = ComposerActions(self.view, self.state, submit, self.notice)
        else:
            self.composer_actions.submit = submit

    async def paste(self, text):
        await self.type_text(text)

    async def type_text(self, text):
        await self._send('\x1b[200~' + text + '\x1b[201~')

    async def click(self, x, y):
        """Send a real SGR mouse press/release at zero-based screen cells."""
        await self._send(f'\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m')

    async def _send(self, text):
        count = self.key_count
        self.input_processed.clear()
        await asyncio.to_thread(self.pipe.send_text, text)
        async def processed():
            while self.key_count == count:
                await self.input_processed.wait()
        await asyncio.wait_for(processed(), 2)
        await self.wait_render()

    async def activate_named(self, name):
        """Reach an actual visible button with Tab, then activate with Enter."""
        caption = {'show_archived': 'Show archived', 'new_session': 'New session',
                   'refresh': 'Refresh', 'more_actions': 'More actions', 'agent_actions': 'Actions', 'clear_draft': 'Clear draft'}[name]
        target = None
        for container in walk(self.application.layout.container, skip_hidden=True):
            if isinstance(container, Window):
                owner = getattr(getattr(container.content, 'text', None), '__self__', None)
                if isinstance(owner, Button) and (owner.text == caption or
                        caption == 'Show archived' and owner.text.startswith(caption + ' [')):
                    target = container.content
        if target is None:
            raise AssertionError('Visible button missing: ' + caption)
        for _ in range(30):
            if self.application.layout.current_control is target:
                await self.key('Enter')
                return
            await self.key('Tab')
        raise AssertionError('Button cannot be reached by Tab: ' + caption)

    async def focus_field(self, name):
        captions = {'launch_mode': 'Launch mode:', 'cwd': 'Working directory',
                    'name': 'Agent name', 'mode': 'History mode [none/literal]:', 'provider': 'Provider:'}
        found, target = False, None
        for container in walk(self.dialogs.body, skip_hidden=True):
            if not isinstance(container, Window):
                continue
            control = container.content
            if found and control.is_focusable():
                target = control
                break
            text = getattr(control, 'text', '')
            rendered = ''.join(fragment[1] for fragment in to_formatted_text(text))
            if rendered.startswith(captions[name]):
                found = True
        if target is None:
            raise AssertionError('Visible field missing: ' + name)
        for _ in range(30):
            if self.application.layout.current_control is target:
                return
            await self.key('Tab')
        raise AssertionError('Field cannot be reached by Tab: ' + name)

    async def select_row(self, ident):
        """Search and activate only after asserting the full highlighted ID."""
        from prompt_toolkit.layout.controls import BufferControl
        for _ in range(30):
            if isinstance(self.application.layout.current_control, BufferControl):
                break
            await self.key('Tab')
        else:
            raise AssertionError('Search is not keyboard reachable')
        await self._send('\x01\x0b')
        await self.paste(ident.swapcase())
        await self.wait_until(lambda: self.state.selected_session_id == ident)
        if self.state.selected_session_id != ident:
            raise AssertionError('Focused stable ID differs from intended target')
        await self.key('Enter')

    async def resize(self, columns, rows):
        before = self.render_count
        self.size = Size(rows=rows, columns=columns)
        self.application._on_resize()
        if self.render_count != before + 1:
            raise AssertionError('resize must capture exactly one synchronous redraw')

    async def close(self):
        self.dialogs.cancel()
        try:
            if not self.task.done() and not self.application.is_done:
                self.application.exit()
            await asyncio.wait_for(self.task, 2)
        finally:
            if not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            (self.client.on_view_change, self.client.output, self.client.on_workspace,
             self.client.on_settings, self.controller.presentation,
             self.controller.on_view_change) = self._old


@asynccontextmanager
async def tui_harness(size=(120, 30), *, client=None, controller=None):
    with create_pipe_input() as pipe:
        ui = TuiHarness(size, client, controller, pipe)
        ui.task = asyncio.create_task(ui.application.run_async())
        try:
            await ui.wait_render()
            if not ui.controller.plain_channel and ui.api is not None:
                ui.view.set_sessions_loading(True)
                response = await asyncio.wait_for(ui.controller.list_sessions(include_archived=True), 2)
                ui.view.set_sessions(response['workspaces'])
                if response.get('warning'):
                    ui.notice(response['warning'])
                ui.view.set_sessions_loading(False)
                await ui.wait_render()
            yield ui
        finally:
            await ui.close()
