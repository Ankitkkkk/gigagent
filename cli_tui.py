"""Full-screen presentation and ownership of interactive CLI resources."""

import asyncio
from contextlib import contextmanager
import signal as signals
import subprocess
import threading

from prompt_toolkit.application import Application, in_terminal
from prompt_toolkit.application.current import set_app
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout

from cli_api import CLIError
from cli_tui_dialogs import DialogHost, TuiWorkflows
from cli_tui_state import TuiState
from cli_tui_view import ComposerActions, TuiView
from cli_view_contracts import ActionOutcome, SubmitOutcome
from cli_workspaces import prepare_attach, resolve_session, run_attach


@contextmanager
def _signal_handlers(loop, callback, notice):
    """Restore process handlers and, on standard asyncio, prior loop callbacks."""
    if threading.current_thread() is not threading.main_thread():
        notice('Signal handling unavailable outside the main thread.')
        yield
        return
    saved = []
    try:
        for signum in (signals.SIGINT, signals.SIGTERM):
            previous = signals.getsignal(signum)
            registration = None
            try:
                handle = getattr(loop, '_signal_handlers', {}).get(signum)
                if handle is not None:
                    function, args, context = handle._callback, handle._args, handle._context
                    if callable(function) and isinstance(args, tuple) and callable(context.run):
                        registration = (context.run, function, *args)
            except (AttributeError, TypeError):
                pass
            try:
                loop.add_signal_handler(signum, callback)
            except (NotImplementedError, RuntimeError, ValueError):
                def forward(*_, loop=loop):
                    loop.call_soon_threadsafe(callback)
                signals.signal(signum, forward)
                installed = False
            else:
                installed = True
            saved.append((signum, previous, registration, installed))
        yield
    finally:
        for signum, previous, registration, installed in reversed(saved):
            if installed:
                loop.remove_signal_handler(signum)
            if registration is not None and installed:
                loop.add_signal_handler(signum, *registration)
            else:
                signals.signal(signum, previous)


async def _wait_owned(task):
    """Finish an owned operation even after repeated cancellation requests."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        return task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


class TuiApplication:
    def __init__(self, client, controller, *, input=None, output=None,
                 terminal_context=in_terminal, runner=subprocess.run, initial_notices=()):
        self.client, self.controller = client, controller
        self.terminal_context, self.runner = terminal_context, runner
        self.state = TuiState()
        for text in initial_notices:
            self.state.notices.add(text)
        # One registry covers spawned workers and temporarily registered callers.
        self._tasks = {}
        self._loop = None
        self._loop_thread = None
        self.quitting = False
        self._quit_task = None
        self._force_quit = False
        self._finished = False
        self._run_error = None
        self._transport_lock = asyncio.Lock()
        self._reconcile_task = None
        self.receiver_task = self.poller_task = None
        self.handoff_lock = asyncio.Lock()
        self.handoff_task = self.attach_task = None
        self._ever_connected = False
        self.dialogs = DialogHost(lambda: self.application, self.invalidate,
                                 owner_is_short_lived=self._dialog_owner)
        self.callbacks = dict(submit=self.submit, quit=self.request_quit,
            navigate=self.navigate, run_action=self.run_action, attach=self._attach_id)
        self.view = TuiView(client, controller, self.state, self.dialogs, self.callbacks)
        self.composer_actions = ComposerActions(self.view, self.state, self.submit, self.notice)
        self.workflows = TuiWorkflows(client, controller, self.view, self.dialogs, self.state,
                                     composer_actions=self.composer_actions)
        bindings = KeyBindings()

        @bindings.add(Keys.SIGINT)
        def interrupt(event):
            self._schedule_signal()

        self.application = Application(layout=Layout(self.view.root, focused_element=self.view.composer),
            input=input, output=output, full_screen=True, mouse_support=True,
            key_bindings=merge_key_bindings([self.view.global_key_bindings, bindings]), style=self.view.style)

    def _dialog_owner(self, task):
        # Quit owns a modal but can later drain navigation: never await it as owner.
        return self._tasks.get(task) in ('startup', 'action', 'navigation', 'dialog')

    def _spawn(self, coroutine, role):
        task = asyncio.create_task(coroutine)
        self._tasks[task] = role
        task.add_done_callback(self._task_done)
        return task

    def _remember_failure(self, error):
        if self._run_error is None:
            self._run_error = error
        elif self._run_error is not error:
            self.notice('Additional local error during shutdown.')

    def _task_done(self, task):
        role = self._tasks.pop(task, None)
        if task.cancelled():
            return
        error = task.exception()  # Retrieve even fire-and-forget owned failures.
        if error is None or (role == 'foreground' and isinstance(error, CLIError)):
            # run_attach's expected refusal is converted by the handoff adapter.
            return
        self._remember_failure(error)
        self._schedule_signal()

    @contextmanager
    def _caller(self, role):
        task = asyncio.current_task()
        previous = self._tasks.get(task)
        self._tasks[task] = role if previous is None else previous
        try:
            yield
        finally:
            if previous is None:
                self._tasks.pop(task, None)
            else:
                self._tasks[task] = previous

    def invalidate(self):
        if hasattr(self, 'application'):
            self.application.invalidate()

    def notice(self, text):
        if self._loop is not None and threading.get_ident() != self._loop_thread:
            self._loop.call_soon_threadsafe(self.notice, text)
            return
        self.state.notices.add(text)
        self.invalidate()

    async def confirm(self, text, *, default=False, escape=False):
        if self.quitting:
            return escape
        async def dialog():
            if self.dialogs.future is not None:
                return escape
            self.view.hide_help()
            return await self.dialogs.confirm(text, default=default, escape=escape)
        return await self._spawn(dialog(), 'dialog')

    async def confirm_selection(self, text, *, workspace, default=False, escape=False):
        if self.quitting:
            return escape
        return await self._spawn(self.workflows.confirm_selection(
            text, workspace=workspace, default=default, escape=escape), 'dialog')

    def _observe(self, event):
        if event.source == 'client' and event.old_channel is not None and event.new_channel is not None:
            self.composer_actions.rename_channel(event.old_channel, event.new_channel)
        self.composer_actions.switch_draft(self.composer_actions.destination_key(), mandatory=True)
        self.view.refresh(event)
        if self.quitting or not self.application.is_running:
            return
        if event.source == 'controller' and event.kind == 'selection':
            self._schedule_reconcile()
        if event.source == 'client' and event.kind == 'connection' and self.client.connection_state == 'connected':
            if self._ever_connected and not self.controller.plain_channel:
                self._spawn(self.workflows.refresh_sessions(), 'action')
            self._ever_connected = True

    def _schedule_reconcile(self):
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = self._spawn(self._sync_transports(), 'reconcile')

    async def _sync_transports(self, *, stop=False):
        async with self._transport_lock:
            while True:
                if self.quitting and not stop:
                    return
                active = not stop and (self.controller.plain_channel or self.controller.workspace is not None)
                wanted = (("receiver_task", active, self.client.receive_forever),
                          ("poller_task", active and not self.controller.plain_channel, self.controller.poll_forever))
                for name, enabled, factory in wanted:
                    task = getattr(self, name)
                    if not enabled and task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        setattr(self, name, None)
                        # Selection may have changed while socket teardown awaited.
                        break
                    if enabled and (task is None or task.done()):
                        setattr(self, name, self._spawn(factory(), 'transport'))
                else:
                    return

    def _admitted(self):
        return not self.quitting and (self._quit_task is None or self._quit_task.done())

    async def navigate(self, mandatory=False):
        if self._quit_task is not None and not self._quit_task.done() and not self.quitting:
            # An archive may finish while the draft question is open. Join as a
            # Quit waiter so the decision cannot cancel/drain its own caller.
            if await self.request_quit():
                return ActionOutcome('cancelled')
        if not self._admitted():
            return ActionOutcome('cancelled')
        with self._caller('navigation'):
            required = mandatory or (not self.controller.plain_channel and self.controller.workspace is None)
            while True:
                await self._sync_transports()
                outcome = await self._guard(self.workflows.navigate(mandatory=mandatory))
                # A cancelled owning picker joined Quit; No must restore that picker.
                if not (required and outcome.status == 'cancelled' and not self.quitting
                        and self.controller.workspace is None):
                    return outcome

    async def run_action(self, action_id, *, target_id=None):
        if not self._admitted():
            return ActionOutcome('cancelled')
        with self._caller('action'):
            return await self._guard(self.workflows.run_action(action_id, target_id=target_id))

    async def _guard(self, operation, outcome_type=ActionOutcome):
        try:
            return await operation
        except Exception as error:
            self._remember_failure(error)
            await self.request_quit(signal=True)
            return outcome_type('failed')

    async def _attach_id(self, agent_id):
        return await self.run_action('attach', target_id=agent_id)

    async def submit(self, text):
        return await self._guard(self._submit(text), SubmitOutcome)

    async def _submit(self, text):
        if not self._admitted():
            return SubmitOutcome('cancelled')
        with self._caller('action'):
            words = text.strip().split(maxsplit=1)
            command = words[0] if words else ''
            if command in ('/archive', '/sessions') and len(words) > 1:
                message = 'unrecognized arguments: ' + words[1]
                self.notice(message)
                return SubmitOutcome('failed', message=message)
            if command in ('/quit', '/exit'):
                return SubmitOutcome('completed', keep_running=False)
            actions = {'/help': 'help', '/agents': 'agents', '/archive': 'archive_session'}
            if command == '/sessions':
                outcome = await self.navigate()
            elif command in actions or (command == '/history' and len(words) == 1):
                outcome = await self.workflows.run_action(actions.get(command, 'history'))
            else:
                if command in ('/join', '/create'):
                    if not self.controller.plain_channel:
                        message = 'Use /sessions to switch sessions, or plain --channel mode to join/create channels.'
                        self.notice(message)
                        return SubmitOutcome('failed', message=message)
                    destination = words[1].strip().removeprefix('#') if len(words) > 1 else ''
                    if not self.state.drafts.can_open(('channel', destination)):
                        self.notice(self.state.drafts.capacity_notice)
                        return SubmitOutcome('cancelled')
                outcome = await self.controller.dispatch_action(text)
                if outcome is None:
                    return await self.client.submit_outcome(text)
                if outcome.status == 'completed' and command == '/rename':
                    await self.workflows.refresh_sessions()
            return SubmitOutcome(outcome.status, message=outcome.message)

    async def attach(self, agent):
        if self.quitting or (self.handoff_task is not None and not self.handoff_task.done()):
            return ActionOutcome('cancelled')
        task = self._spawn(self._attach_owned(dict(agent)), 'handoff')
        self.handoff_task = task  # Before the first await, including preflight/lock waits.
        try:
            return await _wait_owned(task)
        finally:
            if self.handoff_task is task:
                self.handoff_task = None

    async def _attach_owned(self, agent):
        try:
            async with self.handoff_lock:
                prepared = await asyncio.to_thread(prepare_attach, agent, runner=self.runner)
                if self.quitting:
                    return ActionOutcome('cancelled')
                with set_app(self.application):
                    async with self.terminal_context():
                        worker = self._spawn(asyncio.to_thread(run_attach, prepared,
                            runner=self.runner, output=self.notice), 'foreground')
                        self.attach_task = worker
                        try:
                            code = await _wait_owned(worker)
                        finally:
                            self.attach_task = None
                if code:
                    await asyncio.to_thread(prepare_attach, agent, runner=self.runner)
                    raise CLIError('Agent terminal attach failed. Check tmux and retry.')
                return ActionOutcome('completed', agent_id=agent.get('agent_id'))
        except (CLIError, OSError, subprocess.SubprocessError) as error:
            message = str(error) if isinstance(error, CLIError) else 'Could not attach to the agent terminal. Check tmux and retry.'
            self.notice(message)
            return ActionOutcome('failed', message, agent_id=agent.get('agent_id'))

    def _schedule_signal(self):
        if not self._finished:
            self._spawn(self.request_quit(signal=True), 'signal')

    async def request_quit(self, signal=False):
        if self._finished:
            return True
        caller = asyncio.current_task()
        previous = self._tasks.get(caller)
        self._tasks[caller] = 'quit_waiter'
        try:
            if signal:
                self._force_quit = True
                if self._quit_task is not None and not self._quit_task.done():
                    self.quitting = True
                    self._cancel_callers()
                    self.dialogs.cancel()
            if self._quit_task is None or self._quit_task.done():
                self._quit_task = self._spawn(self._quit_owned(), 'quit')
            return await _wait_owned(self._quit_task)
        finally:
            if previous is None:
                self._tasks.pop(caller, None)
            else:
                self._tasks[caller] = previous

    def _cancel_callers(self):
        tasks = [task for task, role in self._tasks.items()
                 if role in ('action', 'navigation', 'startup', 'dialog') and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        return tasks

    async def _quit_owned(self):
        # Enqueue cancellation before resolving the candidate's modal. Its owner
        # must not interpret the dismissal as Chat only and commit a new session.
        preparation = self._spawn(self.controller.cancel_selection(), 'selection_cancel')
        decision_needed = not self._force_quit and bool(len(self.state.drafts))
        shutdown = not decision_needed
        try:
            if shutdown:
                self.quitting = True
                self._cancel_callers()
            self.view.hide_help()
            self.dialogs.cancel()
            if decision_needed:
                # Open inline: no scheduling gap for required navigation to reopen.
                accepted = await self.dialogs.confirm('Quit with unsent drafts? [y/N]', default=False, escape=False)
                await self._settle(_wait_owned(preparation))
                shutdown = accepted or self._force_quit or self._run_error is not None
                if not shutdown:
                    return False
        except Exception as error:
            self._remember_failure(error)
            shutdown = True
        finally:
            if shutdown:
                await self._finish_quit(preparation)
        return True

    async def _settle(self, operation):
        """A local cleanup failure must not skip the remaining owned resources."""
        try:
            await operation
        except Exception as error:
            self._remember_failure(error)

    def _close_view(self):
        for close in (self.view.hide_help, self.dialogs.cancel):
            try:
                close()
            except Exception as error:
                self._remember_failure(error)

    async def _finish_quit(self, preparation):
        self.quitting = True
        try:
            callers = self._cancel_callers()
            await self._settle(_wait_owned(preparation))
            await self._settle(asyncio.gather(*callers, return_exceptions=True))
        finally:
            # Even a failed cancellation/drain cannot restore a still-owned tty.
            try:
                if self.handoff_task is not None:
                    await self._settle(_wait_owned(self.handoff_task))
                await self._settle(self.controller.wait_pending())
                await self._settle(self.controller.close())
            finally:
                try:
                    await self._settle(self._sync_transports(stop=True))
                finally:
                    try:
                        self._close_view()
                    finally:
                        if self.application.is_running and not self.application.is_done:
                            self.application.exit()
                        self._finished = True

    async def _startup(self):
        try:
            if self.controller.plain_channel or self.controller.workspace is not None:
                await self._sync_transports()
            elif self.controller.selector is not None:
                response = await self.controller.list_sessions(include_archived=True)
                self.view.set_sessions(response['workspaces'])
                if response.get('warning'):
                    self.notice(response['warning'])
                selected = resolve_session(response['workspaces'], self.controller.selector)
                outcome = await self.controller.select_session(selected['id'])
                if outcome.message == 'Archived session was not selected':
                    raise CLIError(outcome.message)
                if outcome.status != 'completed':
                    if outcome.status == 'failed':
                        raise CLIError(outcome.message or 'Session selection failed')
                    await self.request_quit(signal=True)
            else:
                await self.navigate(mandatory=True)
        except asyncio.CancelledError:
            if not self.quitting:
                raise
        except Exception as error:
            self._remember_failure(error)
            await self.request_quit(signal=True)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        old = (self.client.output, self.client.on_view_change, self.client.on_workspace,
               self.client.on_settings, self.controller.presentation, self.controller.on_view_change)
        self.client.output = self.notice
        self.client.on_view_change = self._observe
        self.controller.bind_view(self, self._observe)
        app_task = None
        cancelled = False
        try:
            with _signal_handlers(self._loop, self._schedule_signal, self.notice):
                app_task = self._spawn(self.application.run_async(handle_sigint=False,
                    pre_run=lambda: self._spawn(self._startup(), 'startup')), 'lifetime')
                try:
                    await asyncio.shield(app_task)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception as error:
                    self._remember_failure(error)
                finally:
                    await self.request_quit(signal=True)
                    await asyncio.gather(app_task, return_exceptions=True)
        except Exception as error:
            self._remember_failure(error)
        finally:
            self._close_view()
            (self.client.output, self.client.on_view_change, self.client.on_workspace,
             self.client.on_settings, self.controller.presentation, self.controller.on_view_change) = old
            self._loop = None
        if self._run_error is not None:
            raise self._run_error
        if cancelled:
            raise asyncio.CancelledError


async def interactive_tui(client, controller, *, initial_notices=()):
    await TuiApplication(client, controller, initial_notices=initial_notices).run()
