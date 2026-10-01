"""Bounded removal of a known wrapper, without trusting a stale saved PID."""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys


def stop_wrapper_process(pid, root: Path, identity: Path, tmux_name: str | tuple[str, ...],
                         *, owned_process=None):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return
    if owned_process is not None and owned_process.pid == pid:
        if owned_process.poll() is None:
            owned_process.terminate()
            try:
                owned_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                owned_process.kill()
                owned_process.wait(timeout=2)
        return
    # An orphan can survive a server restart. Pin its kernel identity before
    # inspecting argv, so an intervening exit/PID reuse cannot redirect signals.
    if not sys.platform.startswith('linux') or not hasattr(os, 'pidfd_open'):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        raise RuntimeError('Cannot verify this orphan wrapper; stop its original process before removing the agent')
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        try:
            args = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:
            return
        # During exec, Linux can briefly expose an empty cmdline for a live
        # process. Only the pinned exit event proves that an empty row is dead.
        for _ in range(3):
            if any(args):
                break
            if poller.poll(50):
                return
            try:
                args = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
            except FileNotFoundError:
                return
        if not any(args):
            raise RuntimeError('Cannot verify the live wrapper yet; retry removing the agent')
        expected = os.fsencode(str(root / 'wrapper.py'))
        def argument(flag, value):
            try:
                index = args.index(os.fsencode(flag))
                return args[index + 1] == os.fsencode(str(value))
            except (ValueError, IndexError):
                return False
        names = (tmux_name,) if isinstance(tmux_name, str) else tmux_name
        if (len(args) < 2 or args[1] != expected
                or not argument('--identity-file', identity)
                or not any(argument('--tmux-name', name) for name in names)):
            return  # gone/zombie or an unrelated reused PID
        for sig, timeout in ((signal.SIGTERM, 3000), (signal.SIGKILL, 2000)):
            try:
                signal.pidfd_send_signal(fd, sig)
            except ProcessLookupError:
                return
            if poller.poll(timeout):
                return
        raise RuntimeError('Wrapper did not stop; agent was kept for retry')
    finally:
        os.close(fd)
