"""Dependency-preserving retention planning."""

import heapq

from .manifest import committed, created, snapshots


def plan_prune(meta: dict, profile_name: str, keep: int, now_ts: int) -> list[str]:
    """Return snapshot ids to delete, ordered leaf-first (newest first).

    A snapshot is deletable when it is older than *keep*, has no dependents,
    and all of its uploads are complete. Deleting a leaf may make its parent
    deletable, so peel leaves newest-first from the dependency DAG in a single
    pass (a min-heap keyed on ``-created`` keeps the newest leaf at the top).
    """
    if keep <= 0:
        # A non-positive retention window would otherwise delete every
        # snapshot; treat it as "prune nothing" rather than destroy the chain.
        return []
    snaps = [snap for snap in snapshots(meta, profile_name) if snap.get("id")]
    by_id = {snap["id"]: snap for snap in snaps}

    child_count = {snap["id"]: 0 for snap in snaps}
    parent_of: dict[str, str] = {}
    for snap in snaps:
        parent = snap.get("parent")
        if parent in by_id:
            child_count[parent] += 1
            parent_of[snap["id"]] = parent

    def eligible(snap: dict) -> bool:
        return (
            created(snap) > 0
            and now_ts - created(snap) >= keep
            and committed(snap)
        )

    heap: list[tuple[int, str]] = []
    for sid, snap in by_id.items():
        if child_count[sid] == 0 and eligible(snap):
            heapq.heappush(heap, (-created(snap), sid))

    order: list[str] = []
    while heap:
        _, sid = heapq.heappop(heap)
        order.append(sid)
        parent = parent_of.get(sid)
        if parent is None:
            continue
        child_count[parent] -= 1
        if child_count[parent] == 0 and eligible(by_id[parent]):
            heapq.heappush(heap, (-created(by_id[parent]), parent))

    return order
