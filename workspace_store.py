"""Workspace records: the server-owned truth for terminal sessions (spec §1).

One JSON file, written atomically, plus per-agent identity shadows under
identity_dir. Everything that must survive deregister/rename/resume is keyed
by the stable agent_id, never by registry name.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY_MODES = ("none", "literal", "summary")
HISTORY_STATES = ("pending", "done", "failed")
AGENT_STATES = ("starting", "running", "exited", "unknown")
LAUNCH_KINDS = ("spawn", "resume", "fresh")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str) -> str:
    """Channel names must match app._CHANNEL_NAME_RE (20 chars max): 'ws-' + 11 + '-' + 4."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:11].rstrip("-") or "ws")


class WorkspaceStore:
    def __init__(self, path: str | Path, identity_dir: str | Path):
        self._path = Path(path)
        self._identity_dir = Path(identity_dir)
        self._lock = threading.RLock()
        self._workspaces: list[dict] = []
        self._on_change: list = []
        self._on_workspace_change: list = []
        self.warning: str | None = None
        self._load()

    # ---------- persistence ----------

    def _load(self):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("workspaces", []), list):
                raise ValueError("expected an object containing a workspaces list")
            self._workspaces = data.get("workspaces", [])
        except Exception as exc:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            quarantined = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
            try:
                os.replace(self._path, quarantined)
            except OSError:
                pass
            self.warning = (f"workspaces.json was corrupt ({exc}); moved to {quarantined.name}; "
                            "starting with an empty session list")
            log.error(self.warning)
            self._workspaces = []

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"workspaces": self._workspaces}, indent=1, ensure_ascii=False), "utf-8")
        os.replace(tmp, self._path)

    def on_change(self, cb):
        self._on_change.append(cb)

    def on_workspace_change(self, cb):
        self._on_workspace_change.append(cb)

    def _fire(self, ws_id: str | None = None):
        for cb in list(self._on_change):
            try:
                cb()
            except Exception:
                log.exception("workspace on_change callback failed")
        for cb in list(self._on_workspace_change):
            try:
                cb(ws_id)
            except Exception:
                log.exception("workspace on_workspace_change callback failed")

    def _commit(self, ws_id: str | None = None):
        self._save()
        self._fire(ws_id)

    # ---------- workspaces ----------

    def _find(self, ws_id: str) -> dict | None:
        for ws in self._workspaces:
            if ws["id"] == ws_id:
                return ws
        return None

    def create(self, name: str | None) -> dict:
        with self._lock:
            ws_id = "ws_" + uuid.uuid4().hex[:12]
            display = (name or "").strip() or ws_id
            ws = {
                "id": ws_id,
                "name": display,
                "channel": f"ws-{_slug(display)}-{ws_id[3:7]}",
                "archived": False,
                "created_at": _now(),
                "updated_at": _now(),
                "agents": [],
                "routing": {},
            }
            self._workspaces.append(ws)
            try:
                self._commit(ws_id)
            except Exception:
                self._workspaces.pop()
                raise
            return json.loads(json.dumps(ws))

    def get(self, ws_id: str) -> dict | None:
        with self._lock:
            ws = self._find(ws_id)
            return json.loads(json.dumps(ws)) if ws else None

    def list(self, include_archived: bool = False) -> list[dict]:
        with self._lock:
            items = [w for w in self._workspaces if include_archived or not w.get("archived")]
            items = sorted(items, key=lambda w: w.get("updated_at", ""), reverse=True)
            return json.loads(json.dumps(items))

    def resolve(self, id_or_name: str) -> dict | None:
        with self._lock:
            for ws in self._workspaces:
                if ws["id"] == id_or_name:
                    return json.loads(json.dumps(ws))
            exact = [w for w in self._workspaces if w["name"] == id_or_name]
            if len(exact) == 1:
                return json.loads(json.dumps(exact[0]))
            prefix = [w for w in self._workspaces if w["name"].startswith(id_or_name)]
            if len(prefix) == 1:
                return json.loads(json.dumps(prefix[0]))
            return None

    def _touch(self, ws: dict):
        ws["updated_at"] = _now()

    def rename(self, ws_id: str, name: str) -> dict | None:
        with self._lock:
            ws = self._find(ws_id)
            if not ws:
                return None
            ws["name"] = name.strip() or ws["id"]
            self._touch(ws)
            self._commit(ws_id)
            return json.loads(json.dumps(ws))

    def set_archived(self, ws_id: str, archived: bool) -> dict | None:
        with self._lock:
            ws = self._find(ws_id)
            if not ws:
                return None
            ws["archived"] = bool(archived)
            self._touch(ws)
            self._commit(ws_id)
            return json.loads(json.dumps(ws))

    # ---------- agents ----------

    def add_agent(self, ws_id: str, *, provider: str, cwd: str, history_mode: str,
                  registry_name: str, floor_id: int, native_session_id: str | None,
                  history_state: str, last_launch: dict) -> dict:
        if history_mode not in HISTORY_MODES:
            raise ValueError(f"history_mode must be one of {HISTORY_MODES}")
        if history_state not in HISTORY_STATES:
            raise ValueError(f"history_state must be one of {HISTORY_STATES}")
        with self._lock:
            ws = self._find(ws_id)
            if not ws:
                raise KeyError(ws_id)
            agent = {
                "agent_id": "ag_" + uuid.uuid4().hex[:12],
                "provider": provider,
                "registry_name": registry_name,
                "cwd": str(cwd),
                "previous_cwds": [],
                "native_session_id": native_session_id,
                "previous_native_ids": [],
                "native_verified": False,
                "history_mode": history_mode,
                "history_state": history_state,
                "history_note": None,
                "floor_id": floor_id,
                "read_mark": -1,
                "acked_above_mark": [],
                "joined_at": _now(),
                "last_state": "starting",
                "last_error": None,
                "last_launch": dict(last_launch),
            }
            missing = object()
            previous_updated_at = ws.get("updated_at", missing)
            ws["agents"].append(agent)
            self._touch(ws)
            try:
                self._commit(ws_id)
            except Exception:
                ws["agents"].remove(agent)
                if previous_updated_at is missing:
                    ws.pop("updated_at", None)
                else:
                    ws["updated_at"] = previous_updated_at
                raise
            return json.loads(json.dumps(agent))

    def _find_agent(self, ws: dict, agent_id: str) -> dict | None:
        for a in ws["agents"]:
            if a["agent_id"] == agent_id:
                return a
        return None

    def _find_agent_by_id(self, agent_id: str) -> dict | None:
        """Caller must hold self._lock. Live record, not a copy."""
        for ws in self._workspaces:
            a = self._find_agent(ws, agent_id)
            if a:
                return a
        return None

    def get_agent(self, ws_id: str, agent_id: str) -> dict | None:
        with self._lock:
            ws = self._find(ws_id)
            a = self._find_agent(ws, agent_id) if ws else None
            return json.loads(json.dumps(a)) if a else None

    def update_agent(self, ws_id: str, agent_id: str, **fields) -> dict | None:
        with self._lock:
            ws = self._find(ws_id)
            a = self._find_agent(ws, agent_id) if ws else None
            if not a:
                return None
            if "last_state" in fields and fields["last_state"] not in AGENT_STATES:
                raise ValueError(f"last_state must be one of {AGENT_STATES}")
            a.update(fields)
            self._touch(ws)
            self._commit(ws_id)
            return json.loads(json.dumps(a))

    def remove_agent(self, ws_id: str, agent_id: str) -> bool:
        """Only remove a spawn that failed before it ever ran."""
        with self._lock:
            ws = self._find(ws_id)
            if not ws:
                return False
            before = len(ws["agents"])
            ws["agents"] = [a for a in ws["agents"] if a["agent_id"] != agent_id]
            if len(ws["agents"]) == before:
                return False
            self._touch(ws)
            self._commit(ws_id)
            return True

    def update_agent_if_launch(self, ws_id: str, agent_id: str, nonce: str, **fields) -> bool:
        """Compare-and-update under the store lock: write only while the agent's
        current launch nonce is still `nonce`. Background work from an earlier
        launch can never land on a later one (spec §6)."""
        with self._lock:
            ws = self._find(ws_id)
            a = self._find_agent(ws, agent_id) if ws else None
            if not a or (a.get("last_launch") or {}).get("nonce") != nonce:
                return False
            a.update(fields)
            self._touch(ws)
            self._commit(ws_id)
            return True

    _LIVE = ("starting", "running")

    def find_agent_by_registry_name(self, name: str) -> tuple[dict, dict] | None:
        """A live (starting/running) holder wins over an exited one with the same name."""
        with self._lock:
            best = None
            for ws in self._workspaces:
                for a in ws["agents"]:
                    if a["registry_name"] != name:
                        continue
                    if a["last_state"] in self._LIVE:
                        return json.loads(json.dumps(ws)), json.loads(json.dumps(a))
                    best = best or (ws, a)
            if best:
                return json.loads(json.dumps(best[0])), json.loads(json.dumps(best[1]))
            return None

    def rename_agent(self, old: str, new: str) -> int:
        with self._lock:
            n = 0
            renamed_ids = []
            for ws in self._workspaces:
                for a in ws["agents"]:
                    if a["registry_name"] == old:
                        a["registry_name"] = new
                        renamed_ids.append(a["agent_id"])
                        n += 1
            if n:
                self._commit()
            # keep the fail-closed shadow in sync (spec §1); same lock as the
            # rename itself so a concurrent write_identity can't interleave
            # with, or land after, this corrective write.
            for agent_id in renamed_ids:
                shadow = self.read_identity(agent_id)
                if shadow:
                    shadow["registry_name"] = new
                    self._write_identity_dict(agent_id, shadow)
            return n

    def mark_exited(self, registry_name: str, error: str | None = None) -> None:
        with self._lock:
            changed = False
            for ws in self._workspaces:
                for a in ws["agents"]:
                    if a["registry_name"] == registry_name and a["last_state"] in ("starting", "running"):
                        a["last_state"] = "exited"
                        if error:
                            a["last_error"] = error
                        self._touch(ws)
                        changed = True
            if changed:
                self._commit()

    # ---------- membership + recipients ----------

    def members_in_channel(self, channel: str) -> list[dict]:
        with self._lock:
            out = []
            for ws in self._workspaces:
                if ws.get("archived") or ws["channel"] != channel:
                    continue
                for a in ws["agents"]:
                    entry = json.loads(json.dumps(a))
                    entry["workspace_id"] = ws["id"]
                    out.append(entry)
            return out

    def member_names(self, include_archived: bool = True) -> list[str]:
        """Every saved registry name. Archived records count by default: their
        names must never be handed to a new agent, or policy_for could resolve
        the newcomer against the archived record."""
        with self._lock:
            names = []
            for ws in self._workspaces:
                if ws.get("archived") and not include_archived:
                    continue
                names.extend(a["registry_name"] for a in ws["agents"])
            return sorted(set(names))

    def resolve_recipients(self, channel: str, tokens: list[str], router_targets: list[str]) -> list[str]:
        """Spec §4: explicit mentions reach stopped members; broadcasts reach running ones only."""
        members = self.members_in_channel(channel)
        explicit = {t.lower() for t in tokens if t.lower() not in ("all", "both")}
        targets = {t.lower() for t in router_targets}
        ids: list[str] = []
        for m in members:
            rn = m["registry_name"].lower()
            if rn in explicit or m["provider"].lower() in explicit:
                ids.append(m["agent_id"])
            elif m["last_state"] == "running" and rn in targets:
                ids.append(m["agent_id"])
        return sorted(set(ids))

    # ---------- routing table + acks ----------

    def record_routing(self, channel: str, msg_id: int, agent_ids: list[str]) -> None:
        """Record recipients (may be empty) and mark `msg_id` as routed.

        Observers can finish out of order (they await broadcasts), so a plain
        max() watermark would skip a gap after a crash. `routing_done` holds the
        processed ids above `routing_high_water`; compact_routing() advances the
        mark only through ids that are contiguous *in this channel* (message ids
        are global, so the caller supplies the channel's id sequence)."""
        with self._lock:
            for ws in self._workspaces:
                if ws["channel"] == channel and not ws.get("archived"):
                    if agent_ids:
                        ws.setdefault("routing", {})[str(msg_id)] = sorted(set(agent_ids))
                    if int(msg_id) > int(ws.get("routing_high_water", -1)):
                        done = set(ws.setdefault("routing_done", []))
                        done.add(int(msg_id))
                        ws["routing_done"] = sorted(done)
                    self._commit(ws["id"])
                    return

    def routing_high_water(self, ws_id: str) -> int:
        with self._lock:
            ws = self._find(ws_id)
            return int(ws.get("routing_high_water", -1)) if ws else -1

    def unrouted_ids(self, ws_id: str, channel_msg_ids: list[int]) -> list[int]:
        """Channel message ids above the mark that no observer has processed."""
        with self._lock:
            ws = self._find(ws_id)
            if not ws:
                return []
            high = int(ws.get("routing_high_water", -1))
            done = set(ws.get("routing_done", []))
            return [i for i in sorted(channel_msg_ids) if i > high and i not in done]

    def compact_routing(self, ws_id: str, channel_msg_ids: list[int]) -> None:
        """Advance the mark through processed ids that are contiguous in the channel.

        A done id that is no longer in the channel (the observer deletes raw slash
        commands after processing) is dropped so it cannot linger forever."""
        with self._lock:
            ws = self._find(ws_id)
            if not ws:
                return
            high = int(ws.get("routing_high_water", -1))
            known = set(channel_msg_ids)
            newest = max(known, default=-1)
            done = {d for d in ws.get("routing_done", []) if d in known or d > newest}
            moved = done != set(ws.get("routing_done", []))
            for i in sorted(channel_msg_ids):
                if i <= high:
                    continue
                if i in done:
                    high = i
                    done.discard(i)
                    moved = True
                else:
                    break
            if moved:
                ws["routing_high_water"] = high
                ws["routing_done"] = sorted(d for d in done if d > high)
                self._commit(ws_id)

    def routing_for(self, ws_id: str) -> dict[int, list[str]]:
        with self._lock:
            ws = self._find(ws_id)
            if not ws:
                return {}
            return {int(k): list(v) for k, v in ws.get("routing", {}).items()}

    def routed_ids_for(self, ws_id: str, agent_id: str) -> list[int]:
        return sorted(mid for mid, ids in self.routing_for(ws_id).items() if agent_id in ids)

    def _prune_routing_locked(self, ws: dict) -> None:
        if not ws["agents"]:
            return
        low = min(a["read_mark"] for a in ws["agents"])
        routing = ws.get("routing", {})
        for key in [k for k in routing if int(k) <= low]:
            del routing[key]

    def ack(self, ws_id: str, agent_id: str, returned_ids: list[int]) -> None:
        from workspace_unread import apply_acks
        with self._lock:
            ws = self._find(ws_id)
            a = self._find_agent(ws, agent_id) if ws else None
            if not a:
                return
            routed = sorted(int(k) for k, ids in ws.get("routing", {}).items() if agent_id in ids)
            mark, acked = apply_acks(a["read_mark"], a["acked_above_mark"], list(returned_ids), routed)
            if mark == a["read_mark"] and acked == a["acked_above_mark"]:
                return
            a["read_mark"], a["acked_above_mark"] = mark, acked
            self._prune_routing_locked(ws)
            self._commit(ws_id)

    def ack_by_name(self, registry_name: str, channel: str, returned_ids: list[int]) -> None:
        with self._lock:
            for ws in self._workspaces:
                if ws["channel"] != channel or ws.get("archived"):
                    continue
                for a in ws["agents"]:
                    if a["registry_name"] == registry_name:
                        self.ack(ws["id"], a["agent_id"], returned_ids)
                        return

    # ---------- identity shadows ----------

    def identity_path(self, agent_id: str) -> Path:
        return self._identity_dir / f"{agent_id}.json"

    def write_identity(self, ws: dict, agent: dict, token: str) -> Path:
        with self._lock:
            agent_id = agent["agent_id"]
            # Look up the current record rather than trusting the caller's
            # (possibly stale, pre-rename) `agent` dict for registry_name: a
            # racing rename_agent() must never be reverted by a write built
            # from an old snapshot.
            current = self._find_agent_by_id(agent_id)
            registry_name = current["registry_name"] if current else agent["registry_name"]
            data = {
                "registry_name": registry_name,
                "token": token,
                "workspace_id": ws["id"],
                "agent_id": agent_id,
                "channel": ws["channel"],
                "history_mode": agent["history_mode"],
                "floor_id": agent["floor_id"],
                "last_launch": agent.get("last_launch"),
            }
            return self._write_identity_dict(agent_id, data)

    def _write_identity_dict(self, agent_id: str, data: dict) -> Path:
        with self._lock:
            self._identity_dir.mkdir(parents=True, exist_ok=True)
            path = self.identity_path(agent_id)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            return path

    def read_identity(self, agent_id: str) -> dict | None:
        path = self.identity_path(agent_id)
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def restore_identity(self, agent_id: str, data: dict | None) -> None:
        """Restore an identity shadow exactly after a failed relaunch."""
        with self._lock:
            if data is None:
                self.delete_identity(agent_id)
            else:
                self._write_identity_dict(agent_id, data)

    def delete_identity(self, agent_id: str) -> None:
        try:
            self.identity_path(agent_id).unlink()
        except OSError:
            pass

    def _identities(self) -> list[dict]:
        out = []
        if not self._identity_dir.is_dir():
            return out
        for p in self._identity_dir.glob("ag_*.json"):
            try:
                out.append(json.loads(p.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        return out

    # ---------- policy ----------

    def policy_for(self, registry_name: str, channel: str) -> dict | None:
        """(agent_id, floor_id) for a workspace agent reading its workspace channel.

        Store first (a live holder of the name wins over an exited one), identity
        shadow second (spec §1 fail-closed rule), None for anyone else. floor_id
        None means blocked."""
        with self._lock:
            fallback = None
            for ws in self._workspaces:
                if ws["channel"] != channel:
                    continue
                for a in ws["agents"]:
                    if a["registry_name"] != registry_name:
                        continue
                    pol = {"workspace_id": ws["id"], "agent_id": a["agent_id"], "floor_id": a.get("floor_id")}
                    if a["last_state"] in self._LIVE:
                        return pol
                    fallback = fallback or pol
            if fallback:
                return fallback
        for ident in self._identities():
            if ident.get("registry_name") == registry_name and ident.get("channel") == channel:
                return {"workspace_id": ident.get("workspace_id"), "agent_id": ident.get("agent_id"),
                        "floor_id": ident.get("floor_id")}
        return None
