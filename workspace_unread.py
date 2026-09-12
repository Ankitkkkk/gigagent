"""Pure functions for visibility, unread and acknowledgement (spec §1, §4).

No I/O, no locks."""
from __future__ import annotations


BLOCKED_TEXT = ("history policy unavailable for this channel; "
                "ask the user to run /history <agent> <mode>")


def is_blocked(policy: dict | None) -> bool:
    return bool(policy) and policy.get("floor_id") is None


def visible(policy: dict | None, msg: dict) -> bool:
    """Spec §1: id >= floor_id (workspace members only), and an audience-restricted
    message is visible only to an agent whose agent_id is in the audience.

    `policy` is None for an agent that is not a member of the message's
    workspace: no floor applies, but audience still does — a private summary
    never reaches a non-member."""
    audience = (msg.get("metadata") or {}).get("audience")
    if audience is not None and (policy is None or policy.get("agent_id") not in audience):
        return False
    if policy is None:
        return True
    floor = policy.get("floor_id")
    return floor is not None and msg["id"] >= floor


def unread(agent: dict, msgs: list[dict], routing: dict[int, list[str]]) -> list[dict]:
    """Spec §4 definition. `msgs` are the workspace channel's messages, ascending."""
    policy = {"agent_id": agent["agent_id"], "floor_id": agent.get("floor_id")}
    acked = set(agent.get("acked_above_mark") or [])
    out = []
    for m in msgs:
        if m["id"] <= agent["read_mark"] or m["id"] in acked:
            continue
        if m.get("type", "chat") not in ("chat", "summary"):
            continue
        if m.get("sender") == agent["registry_name"]:
            continue
        if agent["agent_id"] not in routing.get(m["id"], []):
            continue
        if not visible(policy, m):
            continue
        out.append(m)
    return out


def bundle_prompt(channel: str, unread_msgs: list[dict]) -> str:
    """Spec §4 'The bundle'. Marker only when more than one message."""
    ids = [m["id"] for m in unread_msgs]
    first = ids[0]
    if len(ids) == 1:
        m = unread_msgs[0]
        return (f"use mcp to read #{channel} with since_id={first - 1} — message #{first} "
                f"from {m.get('sender', '?')} was sent to you and has not been read; "
                f"act on it and respond in #{channel}")
    n = len(ids)
    labels = [f"#{m['id']} ({m.get('sender', '?')})" for m in unread_msgs]
    if n > 50:
        listing = ", ".join(labels[:5] + [f"… and {n - 10} more"] + labels[-5:])
    else:
        listing = ", ".join(labels)
    return (f"While you were away, {n} messages were addressed to you in #{channel}: {listing}. "
            f"Use mcp to read #{channel} with since_id={first - 1} to see them in order, act on "
            f"them, and respond in #{channel}. — End of missed messages: {n} in total, "
            f"nothing else is pending for you.")


def apply_acks(read_mark: int, acked: list[int], returned_ids: list[int],
               routed_ids: list[int]) -> tuple[int, list[int]]:
    """Acknowledge `returned_ids` that are routed to the agent, then compact.

    read_mark: every routed id <= read_mark is acknowledged.
    acked: routed ids above the mark already acknowledged.
    routed_ids: every id routed to this agent (sorted ascending).
    Returns (new_read_mark, new_acked)."""
    routed = sorted(set(routed_ids))
    acked_set = set(acked) | {i for i in returned_ids if i in routed and i > read_mark}
    mark = read_mark
    for rid in routed:
        if rid <= mark:
            continue
        if rid in acked_set:
            mark = rid
            acked_set.discard(rid)
        else:
            break
    return mark, sorted(i for i in acked_set if i > mark)
