from btrbak.retention import plan_prune


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


def test_keep_forever_prunes_nothing():
    meta = _meta(
        [
            _snap("full", 1000),
            _snap("incr1", 2000, "full"),
            _snap("incr2", 3000, "incr1"),
        ]
    )
    assert plan_prune(meta, "p", -1) == []


def test_keep_count_retains_newest_and_their_parents():
    # keep=2 keeps incr2 + incr1, and their parent full, so nothing is pruned:
    # the full is the root of a still-live chain.
    meta = _meta(
        [
            _snap("full", 1000),
            _snap("incr1", 2000, "full"),
            _snap("incr2", 3000, "incr1"),
        ]
    )
    assert plan_prune(meta, "p", 2) == []


def test_prunes_superseded_branch_leaf_first():
    # full1 -> incr1 is a dead branch once full2 re-roots the chain. keep=3
    # retains full2 + incr2 + incr3, so only the old branch prunes, leaf-first.
    meta = _meta(
        [
            _snap("full1", 1000),
            _snap("incr1", 2000, "full1"),
            _snap("full2", 3000),
            _snap("incr2", 4000, "full2"),
            _snap("incr3", 5000, "incr2"),
        ]
    )
    assert plan_prune(meta, "p", 3) == ["incr1", "full1"]


def test_uncommitted_not_pruned_and_protects_parent():
    # incr1's upload never finished, so neither it nor its parent full1 may be
    # pruned, even though full2 is the newest retained snapshot.
    meta = _meta(
        [
            _snap("full1", 1000),
            _snap("incr1", 2000, "full1", status="failed"),
            _snap("full2", 3000),
        ]
    )
    assert plan_prune(meta, "p", 1) == []


def test_local_only_snapshot_is_prunable():
    meta = _meta([_snap("a", 1000, local=True), _snap("b", 2000, local=True)])
    assert plan_prune(meta, "p", 1) == ["a"]


def test_superseded_full_pruned():
    meta = _meta([_snap("full1", 1000), _snap("full2", 2000)])
    assert plan_prune(meta, "p", 1) == ["full1"]


def test_missing_created_is_not_pruned():
    """A hand-edited entry without `created` must not be pruned."""
    meta = _meta(
        [
            {
                "id": "s1",
                "type": "full",
                "uploads": [{"remote": "r", "status": "complete"}],
            },
            _snap("s2", 2000),
        ]
    )
    assert plan_prune(meta, "p", 1) == []


def test_empty_profile_prunes_nothing():
    assert plan_prune(_meta([]), "p", 1) == []


def test_interleaved_chains_prune_independently():
    meta = _meta(
        [
            _snap("a-full", 1000),
            _snap("a-incr", 2000, "a-full"),
            _snap("b-full", 1500),
            _snap("b-incr", 2500, "b-full"),
        ]
    )
    # keep=1 retains the newest snapshot (b-incr) and its parent b-full; the
    # whole a-chain prunes independently, leaf-first within the chain.
    assert plan_prune(meta, "p", 1) == ["a-incr", "a-full"]
