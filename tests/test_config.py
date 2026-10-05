import pytest

from btrbak import config
from btrbak import timespan


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


def test_invalid_yaml_raises_config_error(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    cfg_file.write_text("src: [unclosed\n  foo: bar")
    with pytest.raises(config.ConfigError):
        config.load_config(cfg_file, {})


def test_compression_bool_level_rejected(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "compression": {"algorithm": "xz", "level": True},
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    with pytest.raises(config.ConfigError):
        config.load_config(cfg_file, {})


def test_filter_profiles(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {
                "daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"},
                "weekly": {"freq": {"full": "30d", "incr": "7d"}, "keep": "90d"},
            },
        },
    )
    cfg = config.load_config(cfg_file, {})
    assert set(config.filter_profiles(cfg, "daily").profiles) == {"daily"}
    assert set(config.filter_profiles(cfg, None).profiles) == {"daily", "weekly"}


def test_tmpdir_null_uses_default(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "tmpdir": None,
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    cfg = config.load_config(cfg_file, {})
    assert cfg.tmpdir == config.DEFAULT_TMPDIR


def test_tmpdir_missing_uses_default(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    cfg = config.load_config(cfg_file, {})
    assert cfg.tmpdir == config.DEFAULT_TMPDIR


def test_config_path_for_subvol_prefers_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "root.yaml").write_text("src: /x\n")
    (tmp_path / "root.yml").write_text("src: /x\n")
    assert config.config_path_for_subvol("root") == tmp_path / "root.yaml"


def test_config_path_for_subvol_falls_back_to_yml(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "root.yml").write_text("src: /x\n")
    assert config.config_path_for_subvol("root") == tmp_path / "root.yml"


def test_config_path_for_subvol_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    with pytest.raises(config.ConfigError):
        config.config_path_for_subvol("nope")


def test_discover_configs_prefers_yaml_over_yml(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    _write(
        tmp_path / "root.yaml",
        {
            "src": "/a",
            "dest": "/b",
            "profiles": {
                "daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}
            },
        },
    )
    _write(
        tmp_path / "root.yml",
        {
            "src": "/c",
            "dest": "/d",
            "profiles": {
                "daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}
            },
        },
    )
    _write(
        tmp_path / "other.yml",
        {
            "src": "/e",
            "dest": "/f",
            "profiles": {
                "daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}
            },
        },
    )

    cfgs = config.discover_configs()

    assert [c.name for c in cfgs] == ["other", "root"]
    assert str(cfgs[1].src) == "/a"


def test_filter_profiles_unknown_raises(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    cfg = config.load_config(cfg_file, {})
    with pytest.raises(config.ConfigError):
        config.filter_profiles(cfg, "nope")


def test_select_profiles_returns_none_when_absent(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "/mnt/data",
            "dest": "/mnt/data/.snapshots",
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    cfg = config.load_config(cfg_file, {})
    assert config.select_profiles(cfg, "nope") is None
    assert config.select_profiles(cfg, "daily").name == "root"
    assert set(config.select_profiles(cfg, None).profiles) == {"daily"}


def test_validate_remote_config_unknown_type(tmp_path):
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
                    "remotes": [{"type": "s3", "bucket": "backups"}],
                }
            },
        },
    )
    cfg = config.load_config(cfg_file, {})
    errors = config.validate_remote_config(cfg)
    assert any("unknown remote type" in error for error in errors)
    assert config.validate_remote_config(cfg, "daily") == errors
    assert config.validate_remote_config(cfg, "nope") == ["unknown profile: 'nope'"]


def test_discover_configs_missing_dir_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "does-not-exist")
    with pytest.raises(config.ConfigError):
        config.discover_configs()


def test_discover_configs_empty_dir_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    with pytest.raises(config.ConfigError):
        config.discover_configs()


def _minimal_profile(src="/a", dest="/b"):
    return {"src": src, "dest": dest, "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}}}


def test_discover_configs_tolerant_reports_every_bad_file(tmp_path, monkeypatch):
    """One unparseable profile must not hide the state of every other one."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    _write(tmp_path / "aaa.yaml", _minimal_profile("/a", "/b"))
    (tmp_path / "mmm.yaml").write_text("src: relative/path\ndest: /b\nprofiles: {}\n")
    _write(tmp_path / "zzz.yaml", _minimal_profile("/c", "/d"))

    results = config.discover_configs_tolerant()

    names = [cfg.name if cfg else path.stem for path, cfg, _ in results]
    assert names == ["aaa", "mmm", "zzz"]
    _path, middle_cfg, middle_error = results[1]
    assert middle_cfg is None
    assert "must be an absolute path" in str(middle_error)


def test_discover_configs_tolerant_never_raises_for_bad_content(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "broken.yaml").write_text("profiles: [\n")
    _write(tmp_path / "fine.yaml", _minimal_profile())
    results = config.discover_configs_tolerant()
    assert [cfg is not None for _, cfg, _ in results] == [False, True]
    assert all(
        error is None or isinstance(error, config.ConfigError)
        for _, _, error in results
    )


def test_discover_configs_still_raises_on_the_first_bad_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "aaa.yaml").write_text("profiles: [\n")
    _write(tmp_path / "zzz.yaml", _minimal_profile())
    with pytest.raises(config.ConfigError):
        config.discover_configs()


def test_discover_configs_tolerant_discovery_failure_still_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "does-not-exist")
    with pytest.raises(config.ConfigError):
        config.discover_configs_tolerant()


def test_discover_configs_tolerant_reports_bad_auth_for_every_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "profiles.d")
    (tmp_path / "profiles.d").mkdir()
    auth = tmp_path / "auth.yaml"
    auth.write_text("- not a mapping\n")
    monkeypatch.setattr(config, "AUTH_PATH", auth)
    _write(tmp_path / "profiles.d" / "aaa.yaml", _minimal_profile())
    _write(tmp_path / "profiles.d" / "bbb.yaml", _minimal_profile())
    results = config.discover_configs_tolerant()
    assert [cfg for _, cfg, _ in results] == [None, None]
    assert all(
        "auth file must be a mapping" in str(error) for _, _, error in results
    )


def test_remote_identity_distinguishes_settings():
    a = config.RemoteSpec("offsite", "dir", {"path": "/a"})
    b = config.RemoteSpec("offsite", "dir", {"path": "/b"})
    c = config.RemoteSpec("offsite", "dir", {"path": "/a"})
    assert config.remote_identity(a) != config.remote_identity(b)
    assert config.remote_identity(a) == config.remote_identity(c)


def test_remote_identity_ignores_auth():
    a = config.RemoteSpec("offsite", "dir", {"path": "/a", "auth": {"k": "1"}})
    b = config.RemoteSpec("offsite", "dir", {"path": "/a", "auth": {"k": "2"}})
    assert config.remote_identity(a) == config.remote_identity(b)


def test_validate_errors_on_missing_age_recipient_path(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": str(tmp_path / "src"),
            "dest": str(tmp_path / "dest"),
            "encryption": {
                "algorithm": "age",
                "recipients": ["/nonexistent/recipients.txt"],
            },
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    cfg = config.load_config(cfg_file, {})
    (tmp_path / "src").mkdir()
    errors, _warnings = config.validate(cfg, check_remotes=False)
    assert any(
        "neither an existing file nor an inline age1 key" in error
        for error in errors
    )


def _validate_cfg(tmp_path, dest):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": str(tmp_path / "src"),
            "dest": str(dest),
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    cfg = config.load_config(cfg_file, {})
    (tmp_path / "src").mkdir(exist_ok=True)
    return cfg


def test_validate_rejects_dest_equal_to_src(tmp_path):
    cfg = _validate_cfg(tmp_path, tmp_path / "src")
    errors, _warnings = config.validate(cfg, check_remotes=False)
    assert any("dest must not be the same path as src" in error for error in errors)


def test_validate_rejects_dest_equal_to_src_via_trailing_slash(tmp_path):
    cfg = _validate_cfg(tmp_path, str(tmp_path / "src") + "/")
    errors, _warnings = config.validate(cfg, check_remotes=False)
    assert any("dest must not be the same path as src" in error for error in errors)


def test_validate_nesting_warning_can_be_suppressed(tmp_path, monkeypatch):
    nested = tmp_path / "src" / "snapshots"
    nested.mkdir(parents=True)
    cfg = _validate_cfg(tmp_path, nested)
    # The nesting check lives behind the btrfs/subvolume branch of validate().
    monkeypatch.setattr(config, "is_subvolume", lambda path: True)

    _errors, warnings = config.validate(cfg, check_remotes=False, check_nesting=True)
    assert any("nested inside src" in warning for warning in warnings)

    _errors, warnings = config.validate(cfg, check_remotes=False, check_nesting=False)
    assert not any("nested inside src" in warning for warning in warnings)


# --- path safety ------------------------------------------------------------


@pytest.mark.parametrize(
    "pname",
    [
        "../../../../tmp/pwned",
        "daily/weekly",
        "..",
        ".",
        "",
        "-leading-dash",
        "with space",
        "with\x00null",
        "back\\slash",
    ],
)
def test_load_config_rejects_unsafe_profile_names(tmp_path, pname):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": str(tmp_path / "src"),
            "dest": str(tmp_path / "dest"),
            "profiles": {
                pname: {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}
            },
        },
    )
    with pytest.raises(config.ConfigError, match="profile name"):
        config.load_config(cfg_file, {})


@pytest.mark.parametrize("pname", ["daily", "daily7", "weekly-2024", "a.b_c-d", "x"])
def test_load_config_accepts_safe_profile_names(tmp_path, pname):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": str(tmp_path / "src"),
            "dest": str(tmp_path / "dest"),
            "profiles": {
                pname: {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}
            },
        },
    )
    assert list(config.load_config(cfg_file, {}).profiles) == [pname]


@pytest.mark.parametrize("field", ["src", "dest"])
def test_load_config_requires_absolute_paths(tmp_path, field):
    cfg_file = tmp_path / "root.yaml"
    data = {
        "src": str(tmp_path / "src"),
        "dest": str(tmp_path / "dest"),
        "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
    }
    data[field] = "relative/path"
    _write(cfg_file, data)
    with pytest.raises(config.ConfigError, match=f"'{field}' must be an absolute"):
        config.load_config(cfg_file, {})


def test_load_config_requires_absolute_tmpdir(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": str(tmp_path / "src"),
            "dest": str(tmp_path / "dest"),
            "tmpdir": "relative/tmp",
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    with pytest.raises(config.ConfigError, match="'tmpdir' must be an absolute"):
        config.load_config(cfg_file, {})


def test_load_config_accepts_tilde_paths(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": "~/src",
            "dest": "~/dest",
            "tmpdir": "~/tmp",
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    cfg = config.load_config(cfg_file, {})
    assert cfg.src.is_absolute()
    assert cfg.tmpdir.is_absolute()


def test_empty_tmpdir_still_falls_back_to_default(tmp_path):
    cfg_file = tmp_path / "root.yaml"
    _write(
        cfg_file,
        {
            "src": str(tmp_path / "src"),
            "dest": str(tmp_path / "dest"),
            "tmpdir": "",
            "profiles": {"daily": {"freq": {"full": "7d", "incr": "1d"}, "keep": "30d"}},
        },
    )
    assert config.load_config(cfg_file, {}).tmpdir == config.DEFAULT_TMPDIR
