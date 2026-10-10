"""Count-based, dependency-preserving retention planning."""

import heapq

from .manifest import committed, created, snapshots


def plan_prune(meta: dict, profile_name: str, keep: int) -> list[str]:
    """Return snapshot ids to delete, ordered leaf-first (newest first).

    ``keep`` is a count of how many backups to retain: the ``keep`` most
    recent snapshots are kept, together with every snapshot on their parent
    chains, so a retained incremental can always be restored. ``keep == -1``
    keeps everything forever.

    Only fully committed snapshots are deleted, and a snapshot whose
    ``created`` time is missing or invalid is never deleted (a hand-edited
    entry must not be pruned silently). Uncommitted snapshots -- and their
    ancestors -- are likewise retained until their uploads complete.

    Deletion is leaf-first (newest leaf first): removing a leaf may make its
    parent deletable, so a single pass peels the dependency DAG without ever
    stranding a child on a deleted parent.
    """
    if keep <= 0:
        # -1 means "keep forever"; 0 is rejected by the config layer and is
        # treated defensively as "prune nothing" here.
        return []
    snaps = [snap for snap in snapshots(meta, profile_name) if snap.get("id")]
    if len(snaps) <= keep:
        return []
    by_id = {snap["id"]: snap for snap in snaps}

    # Newest first. Snapshot ids are chronologically sortable, so they serve
    # as a deterministic tie-break when two snapshots share a creation time.
    ordered = sorted(snaps, key=lambda s: (created(s), s["id"]), reverse=True)
    rank = {snap["id"]: index for index, snap in enumerate(ordered)}

    protected = {snap["id"] for snap in ordered[:keep]}

    def protect_chain(sid: str) -> None:
        current = by_id.get(sid)
        while current is not None:
            parent = current.get("parent")
            if parent not in by_id or parent in protected:
                break
            protected.add(parent)
            current = by_id[parent]

    for snap in ordered[:keep]:
        protect_chain(snap["id"])

    # Never delete an uncommitted snapshot or one without a usable creation
    # time, and keep their ancestors so an upload still in flight is never
    # stranded by pruning its send parent.
    for snap in snaps:
        if not committed(snap) or created(snap) <= 0:
            protected.add(snap["id"])
            protect_chain(snap["id"])

    child_count = {snap["id"]: 0 for snap in snaps}
    parent_of: dict[str, str] = {}
    for snap in snaps:
        parent = snap.get("parent")
        if parent in by_id:
            child_count[parent] += 1
            parent_of[snap["id"]] = parent

    def eligible(sid: str) -> bool:
        return sid not in protected and committed(by_id[sid])

    heap: list[tuple[int, str]] = []
    for sid in by_id:
        if child_count[sid] == 0 and eligible(sid):
            heapq.heappush(heap, (rank[sid], sid))

    order: list[str] = []
    while heap:
        _, sid = heapq.heappop(heap)
        order.append(sid)
        parent = parent_of.get(sid)
        if parent is None:
            continue
        child_count[parent] -= 1
        if child_count[parent] == 0 and eligible(parent):
            heapq.heappush(heap, (rank[parent], parent))
    return order
