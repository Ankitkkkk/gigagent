"""Pure functions for visibility, unread and acknowledgement (spec §1, §4).

No I/O, no locks. Task 6 completes this module."""
from __future__ import annotations


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
