"""Dependency-preserving retention planning."""

from manifest import committed, snapshots


def plan_prune(meta: dict, profile_name: str, keep: int, now_ts: int) -> list[str]:
    """Return snapshot ids to delete, ordered leaf-first (newest first).

    A snapshot is deletable when it is older than *keep*, has no dependents,
    and all of its uploads are complete. Deleting a leaf may make its parent
    deletable, so this repeats until a fixed point is reached.
    """
    snaps = list(snapshots(meta, profile_name))
    remaining = {snap["id"] for snap in snaps}
    order: list[str] = []

    while True:
        parents = {
            snap.get("parent")
            for snap in snaps
            if snap["id"] in remaining and snap.get("parent") in remaining
        }
        candidates = [
            snap
            for snap in snaps
            if snap["id"] in remaining
            and now_ts - snap.get("created", 0) >= keep
            and snap["id"] not in parents
            and committed(snap)
        ]
        if not candidates:
            break
        candidates.sort(key=lambda snap: snap.get("created", 0), reverse=True)
        victim = candidates[0]
        remaining.discard(victim["id"])
        order.append(victim["id"])

    return order
