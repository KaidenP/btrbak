import pytest

import config
import timespan


def _write(path, data):
    import yaml

    path.write_text(yaml.safe_dump(data))


def test_load_basic(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {
                "daily": {
                    "freq": {"full": "7d", "incr": "1d"},
                    "keep": "30d",
                    "remotes": [{"type": "dir", "path": "/mnt/offsite"}],
                }
            },
        },
    )
    cfg = config.load_config(cfg_file, {})
    assert cfg.name == "root"
    assert str(cfg.src) == "/mnt/data"
    assert cfg.profiles["daily"].keep == 30 * 86400
    assert cfg.profiles["daily"].freq_full == 7 * 86400
    assert cfg.profiles["daily"].freq_incr == 86400
    assert cfg.profiles["daily"].remotes[0].type == "dir"


def test_auth_is_replaced(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {
                "daily": {
                    "freq": {"full": "7d", "incr": "1d"},
                    "keep": "30d",
                    "remotes": [{"type": "dir", "path": "/mnt/offsite", "auth": "offsite"}],
                }
            },
        },
    )
    cfg = config.load_config(cfg_file, {"offsite": {"username": "btrbak", "password": "x"}})
    settings = cfg.profiles["daily"].remotes[0].settings
    assert settings["auth"] == {"username": "btrbak", "password": "x"}


def test_auth_missing_key(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {
                "daily": {
                    "freq": {"full": "7d", "incr": "1d"},
                    "keep": "30d",
                    "remotes": [{"type": "dir", "path": "/x", "auth": "nope"}],
                }
            },
        },
    )
    with pytest.raises(config.ConfigError):
        config.load_config(cfg_file, {})


def test_remote_stable_id(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {
                "daily": {
                    "freq": {"full": "7d", "incr": "1d"},
                    "keep": "30d",
                    "remotes": [
                        {"type": "dir", "path": "/a"},
                        {"type": "dir", "path": "/b"},
                        {"name": "named", "type": "dir", "path": "/a"},
                    ],
                }
            },
        },
    )
    cfg = config.load_config(cfg_file, {})
    ids = [r.id for r in cfg.profiles["daily"].remotes]
    assert ids[0].startswith("hash:")
    assert ids[0] != ids[1]
    assert ids[2] == "named"


def test_duplicate_remote_rejected(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {
                "daily": {
                    "freq": {"full": "7d", "incr": "1d"},
                    "keep": "30d",
                    "remotes": [
                        {"type": "dir", "path": "/a"},
                        {"type": "dir", "path": "/a"},
                    ],
                }
            },
        },
    )
    with pytest.raises(config.ConfigError):
        config.load_config(cfg_file, {})


def test_freq_never_and_keep_forbidden(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {
                "apt": {"freq": {"full": -1, "incr": -1}, "keep": "90d"}
            },
        },
    )
    cfg = config.load_config(cfg_file, {})
    assert timespan.is_never(cfg.profiles["apt"].freq_full)
    assert timespan.is_never(cfg.profiles["apt"].freq_incr)


def test_keep_never_rejected(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": -1}},
        },
    )
    with pytest.raises(config.ConfigError):
        config.load_config(cfg_file, {})


def test_missing_src_or_dest(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(cfg_file, {"profiles": {"daily": {"freq": {"full": "1d", "incr": "1d"}, "keep": "7d"}}})
    with pytest.raises(config.ConfigError):
        config.load_config(cfg_file, {})
