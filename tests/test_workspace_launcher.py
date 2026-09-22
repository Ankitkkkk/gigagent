"""WorkspaceLauncher with fake process control (spec §2, §4, §6, §7)."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from _workspace_helpers import FakeClock, write_rollout
from agents import AgentTrigger
from providers.base import NullAdapter
from providers.claude import ClaudeAdapter
from providers.codex import CodexAdapter, originator_for
from registry import RuntimeRegistry
from store import MessageStore
from workspace_launcher import LaunchError, TmuxOps, WorkspaceLauncher
from workspace_store import WorkspaceStore


class FakePopen:
    def __init__(self, sink, fail=False):
        self.sink, self.fail = sink, fail

    def __call__(self, cmd, **kw):
        if self.fail:
            raise OSError("cannot exec")
        self.sink.append((list(cmd), kw))

        class P:
            pid = 4242

        return P()


class FakeTmux:
    def remove_session(self, name):
        self.kill_session(name)
        if self.has_session(name):
            raise RuntimeError('session is still running')

    def __init__(self):
        self.sessions = set()
        self.killed = []

    def available(self):
        return True

    def has_session(self, name):
        return name in self.sessions

    def kill_session(self, name):
        self.killed.append(name)
        self.sessions.discard(name)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = Path(self.tmp)
        self.data = root / "data"
        self.data.mkdir()
        self.proj = root / "proj"
        self.proj.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        for name in ("claude", "codex", "kilo"):
            p = self.bin / name
            p.write_text("#!/bin/sh\nsleep 1\n")
            p.chmod(0o755)
        self.config = {
            "server": {"data_dir": str(self.data), "port": 8300},
            "mcp": {"http_port": 8200, "sse_port": 8201},
            "agents": {
                "claude": {"command": str(self.bin / "claude"), "label": "Claude", "color": "#da7756"},
                "codex": {"command": str(self.bin / "codex"), "label": "Codex", "color": "#10a37f"},
                "kilo": {"command": str(self.bin / "kilo"), "label": "Kilo", "color": "#f7f677"},
            },
        }
        self.store = WorkspaceStore(self.data / "workspaces.json", self.data / "identity")
        self.messages = MessageStore(str(self.data / "messages.jsonl"))
        self.registry = RuntimeRegistry(data_dir=str(self.data))
        self.registry.seed(self.config["agents"])
        self.agents = AgentTrigger(self.registry, data_dir=str(self.data))
        self.popen_calls = []
        self.tmux = FakeTmux()
        self.kills = []
        self.clock = FakeClock()
        self.codex_clock = FakeClock()
        self.ws = self.store.create("proj")

        def adapters(name, cfg):
            if name == "claude":
                return ClaudeAdapter(cfg, home=self.home)
            if name == "codex":
                return CodexAdapter(cfg, home=self.home, sleep=self.codex_clock.sleep, clock=self.codex_clock)
            return NullAdapter(name, cfg)

        self.launcher = WorkspaceLauncher(
            store=self.store,
            messages=self.messages,
            registry=self.registry,
            agents=self.agents,
            config=self.config,
            data_dir=self.data,
            root=ROOT,
            popen=FakePopen(self.popen_calls),
            tmux=self.tmux,
            kill=lambda pid, sig: self.kills.append((pid, sig)),
            clock=self.clock,
            sleep=self.clock.sleep,
            python="/usr/bin/python3",
            adapters=adapters,
            which=lambda cmd: cmd if os.path.exists(cmd) else None,
            background=lambda fn, *args: fn(*args),
        )

    def queue(self, name):
        p = self.data / f"{name}_queue.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def agent(self, agent):
        return self.store.get_agent(self.ws["id"], agent["agent_id"])

    def test_validation_has_no_side_effects(self):
        for kwargs, code in (
            (dict(provider="gemini", cwd=str(self.proj), history_mode="literal"), 400),
            (dict(provider="claude", cwd="relative/path", history_mode="literal"), 400),
            (dict(provider="claude", cwd=str(self.proj / "missing"), history_mode="literal"), 400),
            (dict(provider="claude", cwd=str(self.proj), history_mode="summary"), 400),
            (dict(provider="claude", cwd=str(self.proj), history_mode="weird"), 400),
        ):
            with self.assertRaises(LaunchError) as cm:
                self.launcher.spawn(self.ws["id"], **kwargs)
            self.assertEqual(cm.exception.status, code, kwargs)
        self.assertEqual(self.registry.get_all_names(), [])
        self.assertEqual(self.popen_calls, [])
        self.assertEqual(self.store.get(self.ws["id"])["agents"], [])
        identities = list((self.data / "identity").glob("*")) if (self.data / "identity").exists() else []
        self.assertFalse(identities)

    def test_recovery_store_refuses_spawn_and_resume_without_side_effects(self):
        corrupt_data = Path(self.tmp) / "corrupt-data"
        corrupt_data.mkdir()
        (corrupt_data / "workspaces.json").write_text("{bad json")
        store = WorkspaceStore(corrupt_data / "workspaces.json", corrupt_data / "identity")
        ws = store.create("recovered")
        agent = store.add_agent(
            ws["id"], provider="claude", cwd=str(self.proj), history_mode="none",
            registry_name="claude-1", floor_id=0, native_session_id="saved-id",
            history_state="done", last_launch={"kind": "spawn", "nonce": "old", "at": "2026-09-12T00:00:00Z"},
        )
        registry = RuntimeRegistry(data_dir=str(corrupt_data))
        registry.seed(self.config["agents"])
        calls = []
        launcher = WorkspaceLauncher(
            store=store, messages=self.messages, registry=registry,
            agents=AgentTrigger(registry, data_dir=str(corrupt_data)), config=self.config,
            data_dir=corrupt_data, root=ROOT, popen=FakePopen(calls), tmux=FakeTmux(),
            which=lambda cmd: cmd, background=lambda fn, *args: fn(*args),
        )

        for operation in (
            lambda: launcher.spawn(ws["id"], "claude", str(self.proj), "none"),
            lambda: launcher.resume(ws["id"], agent["agent_id"], fresh=True),
        ):
            with self.assertRaises(LaunchError) as cm:
                operation()
            self.assertEqual(cm.exception.status, 409)
            self.assertIn("session store is in recovery", cm.exception.message)

        self.assertEqual(registry.get_all_names(), [])
        self.assertEqual(calls, [])
        self.assertFalse((corrupt_data / "identity").exists())

    def test_bad_adapter_spawn_is_400_before_registration(self):
        from providers import get_adapter
        self.launcher._adapters = get_adapter
        for spec in ("nope:Nope", "providers:MissingClass", "bad-spec"):
            with self.subTest(adapter=spec):
                self.config["agents"]["claude"]["adapter"] = spec
                with patch.object(self.registry, "register", wraps=self.registry.register) as register:
                    with self.assertRaises(LaunchError) as caught:
                        self.launcher.spawn(self.ws["id"], "claude", str(self.proj), "none")
                self.assertEqual(caught.exception.status, 400)
                self.assertIn("adapter for claude could not be loaded:", caught.exception.message)
                register.assert_not_called()
                self.assertEqual(self.registry.get_all_names(), [])
                self.assertEqual(self.registry.free_slot_name("claude"), "claude-1")
                self.assertEqual(self.store.get(self.ws["id"])["agents"], [])
                self.assertEqual(self.popen_calls, [])
                self.assertFalse((self.data / "identity").exists())

    def test_bad_adapter_resume_is_400_without_changes(self):
        from providers import get_adapter
        agent = self.launcher.spawn(self.ws["id"], "claude", str(self.proj), "none")
        self.launcher.stop(self.ws["id"], agent["agent_id"])
        before = self.agent(agent)
        shadow = self.store.read_identity(agent["agent_id"])
        count = len(self.popen_calls)
        self.launcher._adapters = get_adapter
        for spec in ("nope:Nope", "providers:MissingClass", "bad-spec"):
            with self.subTest(adapter=spec):
                self.config["agents"]["claude"]["adapter"] = spec
                with patch.object(self.registry, "register", wraps=self.registry.register) as register:
                    with self.assertRaises(LaunchError) as caught:
                        self.launcher.resume(self.ws["id"], agent["agent_id"], fresh=True)
                self.assertEqual(caught.exception.status, 400)
                self.assertIn("adapter for claude could not be loaded:", caught.exception.message)
                register.assert_not_called()
                self.assertEqual(self.agent(agent), before)
                self.assertEqual(self.store.read_identity(agent["agent_id"]), shadow)
                self.assertEqual(len(self.popen_calls), count)

    def test_spawn_name_race_requests_retry_and_preserves_reason(self):
        from registry import NameInUse
        with patch.object(self.registry, "register", side_effect=NameInUse("claude-1", "grace reserved")):
            with self.assertRaises(LaunchError) as caught:
                self.launcher.spawn(self.ws["id"], "claude", str(self.proj), "none")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("name claude-1 in use; retry", caught.exception.message)
        self.assertIn("grace reserved", caught.exception.message)
        self.assertNotIn("resume", caught.exception.message)

    def test_custom_spawn_and_resume_name_conflicts_preserve_reason(self):
        from registry import NameInUse
        agent = self.launcher.spawn(self.ws["id"], "claude", str(self.proj), "none")
        self.launcher.stop(self.ws["id"], agent["agent_id"])
        for operation, status in (
            (lambda: self.launcher.spawn(self.ws["id"], "claude", str(self.proj), "none", "reviewer"), 400),
            (lambda: self.launcher.resume(self.ws["id"], agent["agent_id"], fresh=True, name="reviewer"), 409),
        ):
            with self.subTest(status=status):
                with patch.object(self.registry, "register", side_effect=NameInUse("reviewer", "family conflict")):
                    with self.assertRaises(LaunchError) as caught:
                        operation()
                self.assertEqual(caught.exception.status, status)
                self.assertIn("family conflict", caught.exception.message)
                if status == 409:
                    self.assertIn("resume with --agent-name <new>", caught.exception.message)

    def test_unread_fetch_starts_after_read_mark(self):
        agent = self.launcher.spawn(self.ws["id"], "claude", str(self.proj), "literal")
        for text in ("seen", "unread", "also unread"):
            msg = self.messages.add("user", text, channel=self.ws["channel"])
            self.store.record_routing(self.ws["channel"], msg["id"], [agent["agent_id"]])
        self.store.ack(self.ws["id"], agent["agent_id"], [0])
        with patch.object(self.messages, "get_since", wraps=self.messages.get_since) as get_since:
            items = self.launcher.unread_for(self.ws["id"], agent["agent_id"])
        self.assertEqual([item["id"] for item in items], [1, 2])
        get_since.assert_called_once_with(0, channel=self.ws["channel"])

    def test_spawn_claude_literal(self):
        ag = self.launcher.spawn(self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="literal")
        self.assertEqual(ag["registry_name"], "claude-1")
        self.assertEqual(ag["last_state"], "starting")
        self.assertEqual(ag["floor_id"], 0)
        self.assertEqual(ag["history_state"], "pending")
        self.assertEqual(ag["last_launch"]["kind"], "spawn")
        self.assertIsNotNone(ag["native_session_id"])
        cmd, kw = self.popen_calls[0]
        self.assertEqual(cmd[:3], ["/usr/bin/python3", str(ROOT / "wrapper.py"), "claude"])
        for flag in ("--no-attach", "--no-restart", "--cwd", "--identity-file", "--tmux-name",
                     "--data-dir", "--port", "--mcp-http-port", "--mcp-sse-port"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--tmux-name") + 1], f"agentchattr-{ag['agent_id']}")
        self.assertEqual(cmd[cmd.index("--session-id") + 1], ag["native_session_id"])
        self.assertTrue(kw["start_new_session"])
        ident = self.store.identity_path(ag["agent_id"])
        self.assertEqual(stat.S_IMODE(ident.stat().st_mode), 0o600)
        self.assertEqual(json.loads(ident.read_text())["floor_id"], 0)
        self.assertEqual(self.registry.get_instance("claude-1")["state"], "active")

    def test_provider_args_persist_and_reach_wrapper_after_separator(self):
        flags = ['--model', 'custom model', '--port', '9999', '--label', 'provider label', '$(touch nope)']
        ag = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'none', provider_args=flags)
        cmd = self.popen_calls[-1][0]
        self.assertEqual(cmd[cmd.index('--') + 1:], ['--session-id', ag['native_session_id'], *flags])
        reloaded = WorkspaceStore(self.data / 'workspaces.json', self.data / 'identity')
        self.assertEqual(reloaded.get_agent(self.ws['id'], ag['agent_id'])['provider_args'], flags)

    def test_resume_reuses_replaces_and_clears_provider_args(self):
        ag = self._running_claude()
        proj = self.home / '.claude' / 'projects' / 'x'
        proj.mkdir(parents=True)
        (proj / f"{ag['native_session_id']}.jsonl").write_text('{}\n')
        self.store.update_agent(self.ws['id'], ag['agent_id'], provider_args=['--model', 'one'])
        for supplied, expected in [(None, ['--model', 'one']), (['--model', 'two'], ['--model', 'two']), ([], [])]:
            self.launcher.stop(self.ws['id'], ag['agent_id'])
            got = self.launcher.resume(self.ws['id'], ag['agent_id'], provider_args=supplied)
            cmd = self.popen_calls[-1][0]
            self.assertEqual(got['provider_args'], expected)
            self.assertEqual(cmd[cmd.index('--') + 1:], ['--resume', ag['native_session_id'], *expected])

    def test_invalid_provider_args_do_not_register_or_launch(self):
        for bad in ('--model one', [1], ['bad\x00arg']):
            with self.assertRaises(LaunchError) as caught:
                self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'none', provider_args=bad)
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.popen_calls, [])
        self.assertEqual(self.registry.get_all_names(), [])

    def test_api_passes_flags_through_spawn_and_fresh_resume(self):
        import asyncio
        import app
        class Request:
            def __init__(self, payload): self.payload = payload
            async def body(self): return json.dumps(self.payload).encode()
        with patch.object(app, 'workspace_launcher', self.launcher):
            got = asyncio.run(app.spawn_agent(self.ws['id'], Request({
                'provider': 'claude', 'cwd': str(self.proj), 'history_mode': 'none',
                'provider_args': ['--model', 'one']})))
            self.assertEqual(got['provider_args'], ['--model', 'one'])
            self.launcher.stop(self.ws['id'], got['agent_id'])
            changed = asyncio.run(app.resume_agent(self.ws['id'], got['agent_id'], Request({
                'fresh': True, 'provider_args': ['--model', 'two']})))
            self.assertEqual(changed['provider_args'], ['--model', 'two'])
            self.assertEqual(self.popen_calls[-1][0][-2:], ['--model', 'two'])

    def test_failed_resume_restores_saved_provider_flags(self):
        ag = self._running_claude()
        self.store.update_agent(self.ws['id'], ag['agent_id'], provider_args=['--model', 'saved'])
        self.launcher.stop(self.ws['id'], ag['agent_id'])
        with patch.object(self.launcher, '_popen', side_effect=OSError('launch failed')):
            with self.assertRaises(LaunchError):
                self.launcher.resume(self.ws['id'], ag['agent_id'], fresh=True, provider_args=['--model', 'new'])
        self.assertEqual(self.agent(ag)['provider_args'], ['--model', 'saved'])

    def test_spawn_none_mode_floor_is_latest_plus_one(self):
        for i in range(3):
            self.messages.add("ankit", f"m{i}", channel=self.ws["channel"])
        ag = self.launcher.spawn(self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="none")
        self.assertEqual(ag["floor_id"], 3)
        self.assertEqual(ag["history_state"], "done")

    def test_spawn_codex_sets_originator_env_and_null_id(self):
        ag = self.launcher.spawn(self.ws["id"], provider="codex", cwd=str(self.proj), history_mode="none")
        self.assertIsNone(ag["native_session_id"])
        cmd, _ = self.popen_calls[0]
        env_items = [cmd[i + 1] for i, token in enumerate(cmd) if token == "--provider-env"]
        self.assertEqual(len(env_items), 1)
        self.assertTrue(env_items[0].startswith("CODEX_INTERNAL_ORIGINATOR_OVERRIDE=agentchattr:" + ag["agent_id"] + ":"))

    def test_custom_name_and_name_in_use(self):
        ag = self.launcher.spawn(
            self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="none", name="reviewer")
        self.assertEqual(ag["registry_name"], "reviewer")
        with self.assertRaises(LaunchError) as cm:
            self.launcher.spawn(
                self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="none", name="reviewer")
        self.assertEqual(cm.exception.status, 400)

    def test_stopped_agent_name_is_never_reused_by_a_new_spawn(self):
        ag = self.launcher.spawn(self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="none")
        self.tmux.sessions.add(f"agentchattr-{ag['agent_id']}")
        self.launcher.on_heartbeat("claude-1", ready=True, pid=1)
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        other = self.store.create("other")
        ag2 = self.launcher.spawn(other["id"], provider="claude", cwd=str(self.proj), history_mode="none")
        self.assertEqual(ag2["registry_name"], "claude-2")
        with self.assertRaises(LaunchError):
            self.launcher.spawn(
                other["id"], provider="claude", cwd=str(self.proj), history_mode="none", name="claude-1")
        self.store.set_archived(self.ws["id"], True)
        ag3 = self.launcher.spawn(other["id"], provider="claude", cwd=str(self.proj), history_mode="none")
        self.assertEqual(ag3["registry_name"], "claude-3")

    def test_popen_failure_on_spawn_removes_entry(self):
        self.launcher._popen = FakePopen(self.popen_calls, fail=True)
        with self.assertRaises(LaunchError) as cm:
            self.launcher.spawn(self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="none")
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(self.store.get(self.ws["id"])["agents"], [])
        self.assertEqual(self.registry.get_all_names(), [])

    def test_add_agent_failure_on_spawn_rolls_back_registration(self):
        with patch.object(self.store, "add_agent", side_effect=OSError("disk full")):
            with self.assertRaises(LaunchError) as cm:
                self.launcher.spawn(
                    self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="none"
                )

        self.assertEqual(cm.exception.status, 500)
        self.assertIn("disk full", cm.exception.message)
        self.assertEqual(self.registry.get_all_names(), [])
        self.assertEqual(self.store.get(self.ws["id"])["agents"], [])
        identities = list((self.data / "identity").glob("*")) if (self.data / "identity").exists() else []
        self.assertEqual(identities, [])

    def test_ready_prompt_names_custom_agent_even_without_history(self):
        for mode in ('none', 'literal'):
            with self.subTest(mode=mode):
                name = 'user-provided-' + mode
                ag = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), mode, name=name)
                self.launcher.on_heartbeat(name, ready=True, pid=77)
                queued = self.queue(name)
                self.assertEqual(len(queued), 1)
                self.assertIn(name, queued[0]['prompt'])
                self.assertIn(self.ws['channel'], queued[0]['prompt'])
                self.assertIn('assigned agent name', queued[0]['prompt'])
                self.assertEqual('since_id=-1' in queued[0]['prompt'], mode == 'literal')
                self.launcher._after_ready(self.ws['id'], ag['agent_id'], ag['last_launch']['nonce'])
                self.assertEqual(len(self.queue(name)), 1)

    def test_identity_enqueue_failure_leaves_startup_retryable(self):
        ag = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'none', name='reviewer')
        with patch.object(self.agents, 'trigger_sync', side_effect=OSError('queue unavailable')):
            self.launcher.on_heartbeat('reviewer', ready=True, pid=77)
        self.assertFalse(self.agent(ag)['last_launch'].get('identity_prompt_sent'))
        self.launcher.on_heartbeat('reviewer', ready=True, pid=77)
        self.assertIn('reviewer', self.queue('reviewer')[0]['prompt'])

    def test_ready_heartbeat_runs_literal_catchup(self):
        ag = self.launcher.spawn(self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="literal")
        self.launcher.on_heartbeat("claude-1", ready=False, pid=1)
        self.assertEqual(self.agent(ag)["last_state"], "starting")
        self.launcher.on_heartbeat("claude-1", ready=True, pid=77)
        got = self.agent(ag)
        self.assertEqual(got["last_state"], "running")
        self.assertEqual(got["last_launch"]["pid"], 77)
        self.assertEqual(got["history_state"], "done")
        q = self.queue("claude-1")
        self.assertEqual(len(q), 1)
        self.assertIn("since_id=-1", q[0]["prompt"])
        self.assertIn("has_more", q[0]["prompt"])

    def test_ready_heartbeat_recovers_exited_wrapper_without_replaying_startup(self):
        ag = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'literal',
                                 provider_args=['--model', 'saved'])
        self.tmux.sessions.add(self.launcher.tmux_name(ag))
        self.launcher.on_heartbeat('claude-1', ready=True, pid=4242)
        before = self.agent(ag)
        queued = self.queue('claude-1')
        self.store.mark_exited('claude-1')

        self.launcher.on_heartbeat('claude-1', ready=True, pid=4242)
        self.launcher.on_heartbeat('claude-1', ready=True, pid=4242)

        self.assertEqual(self.agent(ag), before)
        self.assertEqual(self.queue('claude-1'), queued)

    def test_ready_heartbeat_recovers_legacy_pid_without_replaying_old_startup(self):
        ag = self._running_claude(mode='literal')
        launch = dict(self.agent(ag)['last_launch'])
        for field in ('wrapper_pid', 'identity_prompt_sent', 'startup_delivery_done'):
            launch.pop(field, None)
        before = self.store.update_agent(self.ws['id'], ag['agent_id'], last_launch=launch)
        queued = self.queue('claude-1')
        self.store.mark_exited('claude-1')

        self.launcher.on_heartbeat('claude-1', ready=True, pid=1)

        self.assertEqual(self.agent(ag), before)
        self.assertEqual(self.queue('claude-1'), queued)

    def test_recovered_ready_wrapper_finishes_pending_startup_once(self):
        ag = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'literal')
        self.tmux.sessions.add(self.launcher.tmux_name(ag))
        self.store.mark_exited('claude-1')

        self.launcher.on_heartbeat('claude-1', ready=True, pid=4242)
        self.launcher.on_heartbeat('claude-1', ready=True, pid=4242)

        got = self.agent(ag)
        self.assertEqual(got['last_state'], 'running')
        self.assertEqual(got['last_launch']['nonce'], ag['last_launch']['nonce'])
        self.assertEqual(got['history_state'], 'done')
        self.assertEqual(len(self.queue('claude-1')), 1)
        self.assertIn('since_id=-1', self.queue('claude-1')[0]['prompt'])

    def test_exited_heartbeat_recovery_requires_same_ready_wrapper_and_live_session(self):
        ag = self._running_claude()
        launch = dict(self.agent(ag)['last_launch'])
        for label, pid, ready, present, archived, expected_pid in (
                ('missing pid', None, True, True, False, 4242),
                ('zero pid', 0, True, True, False, 0),
                ('negative pid', -1, True, True, False, -1),
                ('boolean pid', True, True, True, False, True),
                ('string pid', '4242', True, True, False, 4242),
                ('mismatched pid', 99, True, True, False, 4242),
                ('provider pid cannot override wrapper', 1, True, True, False, 4242),
                ('not ready', 4242, False, True, False, 4242),
                ('missing session', 4242, True, False, False, 4242),
                ('archived', 4242, True, True, True, 4242)):
            with self.subTest(label=label):
                self.store.set_archived(self.ws['id'], archived)
                self.tmux.sessions = {self.launcher.tmux_name(ag)} if present else set()
                before = self.store.update_agent(self.ws['id'], ag['agent_id'], last_state='exited',
                    last_launch=dict(launch, wrapper_pid=expected_pid))
                self.launcher.on_heartbeat('claude-1', ready=ready, pid=pid)
                self.assertEqual(self.agent(ag), before)

    def test_stopped_wrapper_cannot_recover_from_late_ready_heartbeat(self):
        ag = self._running_claude()
        with patch.object(self.tmux, 'kill_session'):
            self.launcher.stop(self.ws['id'], ag['agent_id'])
        before = self.agent(ag)
        self.launcher.on_heartbeat('claude-1', ready=True, pid=4242)
        self.assertEqual(self.agent(ag), before)

    def test_tmux_presence_requires_exact_session_name(self):
        def run(command, **kwargs):
            target = command[-1]
            names = {'agentchattr-ag_one-extra'}
            found = (target[1:] in names if target.startswith('=') else
                     any(name.startswith(target) for name in names))
            return subprocess.CompletedProcess(command, 0 if found else 1)

        with patch('workspace_launcher.subprocess.run', side_effect=run):
            self.assertFalse(TmuxOps().has_session('agentchattr-ag_one'))
            self.assertTrue(TmuxOps().has_session('agentchattr-ag_one-extra'))

    def test_stale_literal_catchup_cas_never_enqueues(self):
        ag = self.launcher.spawn(
            self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="literal"
        )
        nonce = ag["last_launch"]["nonce"]

        with patch.object(self.store, "update_agent_if_launch", return_value=False):
            self.launcher._after_ready(self.ws["id"], ag["agent_id"], nonce)

        self.assertEqual(self.queue("claude-1"), [])
        self.assertEqual(self.agent(ag)["history_state"], "pending")

    def test_literal_catchup_enqueue_failure_reverts_pending(self):
        ag = self.launcher.spawn(
            self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="literal"
        )
        nonce = ag["last_launch"]["nonce"]

        with patch.object(self.agents, "trigger_sync", side_effect=OSError("queue read only")):
            with self.assertLogs("workspace_launcher", level="ERROR") as logs:
                self.launcher._after_ready(self.ws["id"], ag["agent_id"], nonce)

        self.assertEqual(self.agent(ag)["history_state"], "pending")
        self.assertTrue(any("queue read only" in line for line in logs.output))

    def test_no_ready_within_timeout_terminates_launch(self):
        ag = self.launcher.spawn(self.ws["id"], provider="claude", cwd=str(self.proj), history_mode="none")
        self.tmux.sessions.add(f"agentchattr-{ag['agent_id']}")
        ident = self.store.identity_path(ag["agent_id"])
        last_launch = dict(self.agent(ag)["last_launch"])
        last_launch["wrapper_pid"] = 555
        self.store.update_agent(self.ws["id"], ag["agent_id"], last_launch=last_launch)
        self.clock.t += 61
        self.launcher.tick()
        got = self.agent(ag)
        self.assertEqual(got["last_state"], "exited")
        self.assertIn("ready", got["last_error"])
        self.assertIn(f"agentchattr-{ag['agent_id']}", self.tmux.killed)
        self.assertIn((555, 15), self.kills)
        self.assertIsNone(self.registry.get_instance("claude-1"))
        self.assertTrue(ident.exists())

    def test_stale_discovery_result_is_dropped_after_relaunch(self):
        ag = self.launcher.spawn(self.ws["id"], provider="codex", cwd=str(self.proj), history_mode="none")
        old_nonce = self.agent(ag)["last_launch"]["nonce"]
        old_launch = self.launcher.launch_context_for(self.agent(ag))
        write_rollout(self.home, "11111111-1111-4111-8111-111111111111", originator_for(old_launch), str(self.proj))
        self.tmux.sessions.add(f"agentchattr-{ag['agent_id']}")
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        self.launcher.resume(self.ws["id"], ag["agent_id"], fresh=True)
        self.launcher._after_ready(self.ws["id"], ag["agent_id"], old_nonce)
        self.assertIsNone(self.agent(ag)["native_session_id"])

    def test_codex_discovery_after_ready(self):
        ag = self.launcher.spawn(self.ws["id"], provider="codex", cwd=str(self.proj), history_mode="none")
        launch = self.launcher.launch_context_for(self.agent(ag))
        write_rollout(self.home, "11111111-1111-4111-8111-111111111111", originator_for(launch), str(self.proj))
        self.launcher.on_heartbeat("codex-1", ready=True, pid=1)
        got = self.agent(ag)
        self.assertEqual(got["native_session_id"], "11111111-1111-4111-8111-111111111111")
        self.assertTrue(got["native_verified"])

    def _running_claude(self, mode="none"):
        ag = self.launcher.spawn(self.ws["id"], provider="claude", cwd=str(self.proj), history_mode=mode)
        self.tmux.sessions.add(f"agentchattr-{ag['agent_id']}")
        self.launcher.on_heartbeat("claude-1", ready=True, pid=1)
        return self.agent(ag)

    def test_stop_kills_and_marks_exited_keeps_state(self):
        ag = self._running_claude()
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        got = self.agent(ag)
        self.assertEqual(got["last_state"], "exited")
        self.assertEqual(got["native_session_id"], ag["native_session_id"])
        self.assertIn(f"agentchattr-{ag['agent_id']}", self.tmux.killed)
        self.assertTrue(self.store.identity_path(ag["agent_id"]).exists())

    def test_resume_refusals(self):
        ag = self._running_claude()
        with self.assertRaises(LaunchError) as cm:
            self.launcher.resume(self.ws["id"], ag["agent_id"])
        self.assertEqual(cm.exception.status, 409)
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        with self.assertRaises(LaunchError) as cm:
            self.launcher.resume(self.ws["id"], ag["agent_id"])
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("--fresh", cm.exception.message)
        self.store.update_agent(self.ws["id"], ag["agent_id"], native_session_id=None)
        with self.assertRaises(LaunchError) as cm:
            self.launcher.resume(self.ws["id"], ag["agent_id"])
        self.assertIn("--fresh", cm.exception.message)
        kilo = self.launcher.spawn(self.ws["id"], provider="kilo", cwd=str(self.proj), history_mode="none")
        self.launcher.stop(self.ws["id"], kilo["agent_id"])
        with self.assertRaises(LaunchError) as cm:
            self.launcher.resume(self.ws["id"], kilo["agent_id"])
        self.assertIn("adapter", cm.exception.message)

    def test_resume_live_terminal_refusal_recommends_attach(self):
        ag = self._running_claude()
        self.store.mark_exited('claude-1')
        with self.assertRaises(LaunchError) as caught:
            self.launcher.resume(self.ws['id'], ag['agent_id'])
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('already running', caught.exception.message)
        self.assertIn('Attach', caught.exception.message)

    def test_resume_success_and_bundle(self):
        ag = self._running_claude()
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        sid = ag["native_session_id"]
        proj = self.home / ".claude" / "projects" / "x"
        proj.mkdir(parents=True)
        (proj / f"{sid}.jsonl").write_text("{}\n")
        m1 = self.messages.add("ankit", "@claude-1 first", channel=self.ws["channel"])
        m2 = self.messages.add("ankit", "@claude-1 second", channel=self.ws["channel"])
        self.store.record_routing(self.ws["channel"], m1["id"], [ag["agent_id"]])
        self.store.record_routing(self.ws["channel"], m2["id"], [ag["agent_id"]])
        self.popen_calls.clear()
        got = self.launcher.resume(self.ws["id"], ag["agent_id"])
        self.assertEqual(got["last_state"], "starting")
        self.assertEqual(got["last_launch"]["kind"], "resume")
        self.assertEqual(got["registry_name"], "claude-1")
        cmd, _ = self.popen_calls[0]
        self.assertEqual(cmd[cmd.index("--resume") + 1], sid)
        self.launcher.on_heartbeat("claude-1", ready=True, pid=2)
        q = self.queue("claude-1")
        self.assertEqual(len(q), 3)  # Spawn identity, resume identity, then unread bundle.
        self.assertIn("claude-1", q[-2]["prompt"])
        self.assertIn("While you were away, 2 messages", q[-1]["prompt"])
        self.assertIn(f"since_id={m1['id'] - 1}", q[-1]["prompt"])
        self.launcher._after_ready(self.ws['id'], ag['agent_id'], got['last_launch']['nonce'])
        self.assertEqual(len(self.queue('claude-1')), 3)

    def test_failed_startup_bundle_retries_without_repeating_identity(self):
        ag = self.launcher.spawn(self.ws['id'], 'claude', str(self.proj), 'none')
        launch = dict(ag['last_launch'], kind='resume')
        self.store.update_agent(self.ws['id'], ag['agent_id'], last_launch=launch)
        with patch.object(self.launcher, '_send_bundle', side_effect=[OSError('queue unavailable'), 0]) as send:
            with self.assertLogs('workspace_launcher', level='ERROR'):
                self.launcher.on_heartbeat('claude-1', ready=True, pid=77)
            self.launcher.on_heartbeat('claude-1', ready=True, pid=77)
            self.launcher.on_heartbeat('claude-1', ready=True, pid=77)
            self.assertEqual(send.call_count, 2)
        self.assertEqual(len(self.queue('claude-1')), 1)

    def test_resume_consumes_pending_literal_catchup_once(self):
        ag = self._running_claude(mode="literal")
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        proj = self.home / ".claude" / "projects" / "x"
        proj.mkdir(parents=True)
        (proj / f"{ag['native_session_id']}.jsonl").write_text("{}\n")
        self.store.update_agent(self.ws["id"], ag["agent_id"], history_state="pending")
        before = len(self.queue("claude-1"))

        self.launcher.resume(self.ws["id"], ag["agent_id"])
        self.launcher.on_heartbeat("claude-1", ready=True, pid=2)
        self.assertEqual(self.agent(ag)["history_state"], "done")
        self.assertEqual(len(self.queue("claude-1")), before + 1)
        self.assertIn("since_id=-1", self.queue("claude-1")[-1]["prompt"])

        self.launcher.stop(self.ws["id"], ag["agent_id"])
        self.launcher.resume(self.ws["id"], ag["agent_id"])
        self.launcher.on_heartbeat("claude-1", ready=True, pid=3)
        self.assertEqual(len(self.queue("claude-1")), before + 2)
        self.assertNotIn("since_id=-1", self.queue("claude-1")[-1]["prompt"])

    def test_resume_name_in_use_and_rename(self):
        ag = self._running_claude()
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        proj = self.home / ".claude" / "projects" / "x"
        proj.mkdir(parents=True)
        (proj / f"{ag['native_session_id']}.jsonl").write_text("{}\n")
        self.registry.register("claude", preferred_name="claude-1", allow_reserved=True)
        with self.assertRaises(LaunchError) as cm:
            self.launcher.resume(self.ws["id"], ag["agent_id"])
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("name claude-1 in use", cm.exception.message)
        self.assertIn("resume with --agent-name <new>", cm.exception.message)
        got = self.launcher.resume(self.ws["id"], ag["agent_id"], name="claude-2")
        self.assertEqual(got["registry_name"], "claude-2")
        self.assertEqual(got["floor_id"], ag["floor_id"])

    def test_fresh_allocates_new_id_and_reruns_history(self):
        ag = self._running_claude(mode="literal")
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        old = ag["native_session_id"]
        self.popen_calls.clear()
        got = self.launcher.resume(self.ws["id"], ag["agent_id"], fresh=True)
        self.assertNotEqual(got["native_session_id"], old)
        self.assertEqual(got["previous_native_ids"], [old])
        self.assertEqual(got["last_launch"]["kind"], "fresh")
        self.assertEqual(got["history_state"], "pending")
        cmd, _ = self.popen_calls[0]
        self.assertIn("--session-id", cmd)
        self.launcher.on_heartbeat("claude-1", ready=True, pid=3)
        self.assertEqual(self.agent(ag)["history_state"], "done")
        self.assertTrue(any("since_id=-1" in item.get("prompt", "") for item in self.queue("claude-1")))

    def test_fresh_works_for_null_adapter(self):
        kilo = self.launcher.spawn(self.ws["id"], provider="kilo", cwd=str(self.proj), history_mode="none")
        self.launcher.stop(self.ws["id"], kilo["agent_id"])
        got = self.launcher.resume(self.ws["id"], kilo["agent_id"], fresh=True)
        self.assertEqual(got["last_state"], "starting")

    def test_resume_cwd_repoint(self):
        ag = self._running_claude()
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        proj = self.home / ".claude" / "projects" / "x"
        proj.mkdir(parents=True)
        (proj / f"{ag['native_session_id']}.jsonl").write_text("{}\n")
        calls_before = len(self.popen_calls)
        with self.assertRaises(LaunchError) as cm:
            self.launcher.resume(self.ws["id"], ag["agent_id"], cwd="relative/path")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(len(self.popen_calls), calls_before)
        self.assertEqual(self.registry.get_all_names(), [])
        with self.assertRaises(LaunchError):
            self.launcher.resume(self.ws["id"], ag["agent_id"], cwd=str(Path(self.tmp) / "nope"))
        new = Path(self.tmp) / "proj2"
        new.mkdir()
        got = self.launcher.resume(self.ws["id"], ag["agent_id"], cwd=str(new))
        self.assertEqual(got["cwd"], str(new))
        self.assertEqual(got["previous_cwds"], [str(self.proj)])

    def test_popen_failure_on_resume_keeps_state_and_shadow(self):
        ag = self._running_claude()
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        proj = self.home / ".claude" / "projects" / "x"
        proj.mkdir(parents=True)
        (proj / f"{ag['native_session_id']}.jsonl").write_text("{}\n")
        original = self.agent(ag)
        original_shadow = self.store.read_identity(ag["agent_id"])
        self.launcher._popen = FakePopen(self.popen_calls, fail=True)
        with self.assertRaises(LaunchError):
            self.launcher.resume(self.ws["id"], ag["agent_id"])
        got = self.agent(ag)
        self.assertEqual(got["last_state"], "exited")
        self.assertEqual(got["native_session_id"], ag["native_session_id"])
        self.assertEqual(got["last_launch"], original["last_launch"])
        self.assertEqual(self.store.read_identity(ag["agent_id"]), original_shadow)
        self.assertIn("cannot exec", got["last_error"])

        before_fresh = self.agent(ag)
        self.store.delete_identity(ag["agent_id"])
        shadow_before_fresh = self.store.read_identity(ag["agent_id"])
        self.assertIsNone(shadow_before_fresh)
        with self.assertRaises(LaunchError):
            self.launcher.resume(self.ws["id"], ag["agent_id"], fresh=True, name="claude-9")
        got = self.agent(ag)
        self.assertEqual(got["native_session_id"], ag["native_session_id"])
        self.assertEqual(got["previous_native_ids"], [])
        self.assertEqual(got["registry_name"], "claude-1")
        self.assertEqual(got["last_launch"], before_fresh["last_launch"])
        self.assertEqual(self.store.read_identity(ag["agent_id"]), shadow_before_fresh)

    def test_identity_write_failure_on_fresh_restores_saved_conversation(self):
        ag = self._running_claude()
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        before = self.agent(ag)
        shadow = self.store.read_identity(ag["agent_id"])

        with patch.object(self.store, "write_identity", side_effect=OSError("read only")):
            with self.assertRaises(LaunchError) as cm:
                self.launcher.resume(self.ws["id"], ag["agent_id"], fresh=True)

        got = self.agent(ag)
        self.assertEqual(cm.exception.status, 500)
        self.assertIn("read only", cm.exception.message)
        self.assertEqual(got["native_session_id"], before["native_session_id"])
        self.assertEqual(got["last_launch"], before["last_launch"])
        self.assertEqual(self.store.read_identity(ag["agent_id"]), shadow)
        self.assertEqual(self.registry.get_all_names(), [])

    def test_reconcile_marks_missing_sessions_exited(self):
        ag = self._running_claude()
        self.tmux.sessions.clear()
        self.launcher.reconcile()
        self.assertEqual(self.agent(ag)["last_state"], "exited")

    def test_checkpoint_verifies_transcript(self):
        ag = self._running_claude()
        self.assertFalse(self.agent(ag)["native_verified"])
        proj = self.home / ".claude" / "projects" / "x"
        proj.mkdir(parents=True)
        (proj / f"{ag['native_session_id']}.jsonl").write_text("{}\n")
        result = self.launcher.checkpoint(self.ws["id"])
        self.assertTrue(self.agent(ag)["native_verified"])
        self.assertEqual(result["checked"], 1)

    def test_retry_rules(self):
        ag = self._running_claude()
        with self.assertRaises(LaunchError) as cm:
            self.launcher.retry(self.ws["id"], ag["agent_id"])
        self.assertEqual(cm.exception.status, 400)
        m = self.messages.add("ankit", "@claude-1 hi", channel=self.ws["channel"])
        self.store.record_routing(self.ws["channel"], m["id"], [ag["agent_id"]])
        unread_ids = [item["id"] for item in self.launcher.unread_for(self.ws["id"], ag["agent_id"])]
        self.assertEqual(unread_ids, [m["id"]])
        self.launcher.retry(self.ws["id"], ag["agent_id"])
        self.assertIn(f"message #{m['id']}", self.queue("claude-1")[-1]["prompt"])
        self.launcher.stop(self.ws["id"], ag["agent_id"])
        with self.assertRaises(LaunchError) as cm:
            self.launcher.retry(self.ws["id"], ag["agent_id"])
        self.assertEqual(cm.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
