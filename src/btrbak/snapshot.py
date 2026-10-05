"""btrfs subvolume snapshot management."""

from .util import run


def create_ro_snapshot(src, dest) -> None:
    """Create a read-only snapshot of *src* at *dest*."""
    run(["btrfs", "subvolume", "snapshot", "-r", str(src), str(dest)])


def delete_snapshot(path) -> None:
    """Delete a btrfs snapshot subvolume."""
    run(["btrfs", "subvolume", "delete", str(path)])
