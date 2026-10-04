from retention import plan_prune


def _snap(sid, created, parent=None, status="complete", local=False):
    if local:
        uploads = []
    elif status is None:
        uploads = []
    else:
        uploads = [{"remote": "r", "status": status}]
    return {
        "id": sid,
        "created": created,
        "type": "local" if local else ("incr" if parent else "full"),
        "parent": parent,
        "uploads": uploads,
    }


def _meta(snaps):
    return {"profiles": {"p": {"snapshots": snaps}}}


def test_prune_chain_leaf_first():
    meta = _meta(
        [
            _snap("full", 1000),
            _snap("incr1", 2000, "full"),
            _snap("incr2", 3000, "incr1"),
        ]
    )
    assert plan_prune(meta, "p", 10, 5000) == ["incr2", "incr1", "full"]


def test_young_child_protects_parent():
    meta = _meta(
        [
            _snap("full", 1000),
            _snap("incr1", 4950, "full"),
        ]
    )
    assert plan_prune(meta, "p", 100, 5000) == []


def test_incomplete_not_pruned():
    meta = _meta([_snap("full", 1000, status="failed")])
    assert plan_prune(meta, "p", 10, 5000) == []


def test_local_only_prunes_by_age():
    meta = _meta([_snap("a", 1000, local=True), _snap("b", 4950, local=True)])
    assert plan_prune(meta, "p", 100, 5000) == ["a"]


def test_orphan_full_without_children_deleted():
    meta = _meta([_snap("full", 1000)])
    assert plan_prune(meta, "p", 10, 5000) == ["full"]
