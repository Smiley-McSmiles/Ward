import json
import os

import pytest

from ward import deps, log, paths, schema, util
from ward.settings import Settings


def test_paths_follow_ward_home(ward_home):
    assert paths.config_dir() == str(ward_home / "config")
    assert paths.db_file().startswith(str(ward_home / "data"))
    assert paths.cache_dir() == str(ward_home / "cache")


def test_paths_follow_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("WARD_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/is/ignored")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.config_dir() == str(tmp_path / "cfg" / "ward")
    assert paths.data_dir() == str(tmp_path / ".local" / "share" / "ward")


def test_every_visible_setting_is_complete():
    pages = {p for p, _t, _i in schema.PAGES}
    for key, spec in schema.SPECS.items():
        assert schema.coerce(spec, spec.default) == spec.default, key
        if spec.page is None:
            continue
        assert spec.page in pages, key
        assert spec.title and spec.group, key
        if spec.type == "choice":
            assert spec.default in [c for c, _ in spec.choices], key
    for key, spec in schema.ACCOUNT_SPECS.items():
        assert schema.coerce(spec, spec.default) == spec.default, key


def test_coerce_rejects_bad_values():
    with pytest.raises(ValueError):
        schema.coerce(schema.SPECS["appearance.text_scale"], 500)
    with pytest.raises(ValueError):
        schema.coerce(schema.SPECS["appearance.style"], "purple")
    assert schema.coerce(schema.SPECS["encryption.inactive_days"], "90") == 90
    assert schema.coerce(schema.SPECS["general.background"], "off") is False
    with pytest.raises(ValueError):
        schema.coerce(schema.SPECS["notify.quiet_hours"], "25:00-07:00")


def test_time_ranges():
    assert schema.in_time_range("22:00-07:00", 23 * 60)
    assert schema.in_time_range("22:00-07:00", 6 * 60)
    assert not schema.in_time_range("22:00-07:00", 12 * 60)
    assert schema.in_time_range("09:00-17:00", 12 * 60)
    assert not schema.in_time_range("", 12 * 60)


def test_settings_roundtrip_and_signals(ward_home):
    s = Settings()
    seen = []
    s.connect("changed::appearance.style", lambda _s, key: seen.append(key))
    assert s["appearance.style"] == "system"
    assert s.set("appearance.style", "dark") is True
    assert s.set("appearance.style", "dark") is False
    s.set("chats.quick_reactions", ["👍", "🔥"])
    s.flush()
    assert seen == ["appearance.style"]
    with open(paths.settings_file()) as fh:
        on_disk = json.load(fh)
    assert on_disk["appearance.style"] == "dark"
    assert "general.background" not in on_disk          # defaults are not written
    s2 = Settings()
    assert s2["appearance.style"] == "dark"
    assert s2["chats.quick_reactions"] == ["👍", "🔥"]
    s2.reset()
    assert s2["appearance.style"] == "system"


def test_settings_ignore_garbage(ward_home):
    os.makedirs(paths.config_dir(), exist_ok=True)
    with open(paths.settings_file(), "w") as fh:
        json.dump({"appearance.text_scale": 9999, "unknown.key": 1, "notify.enabled": False}, fh)
    s = Settings()
    assert s["appearance.text_scale"] == 100
    assert s["notify.enabled"] is False


def test_util_helpers():
    assert util.valid_bare_jid("alice@example.org")
    assert util.valid_bare_jid("example.org")
    assert not util.valid_bare_jid("alice@example.org/phone")
    assert not util.valid_bare_jid("@example.org")
    assert not util.valid_bare_jid("al ice@example.org")
    assert util.bare("Alice@Example.org/res") == "alice@example.org"
    assert util.resource("a@b/c/d") == "c/d"
    assert util.human_size(1500) == "1.5 KB"
    assert util.random_resource().startswith("ward.")
    assert util.first_line("\n\n  hello\nworld") == "hello"


def test_log_masking():
    assert "secret" not in log.mask("<auth mechanism='PLAIN'>c2VjcmV0</auth>")
    assert log.mask("aesgcm://h/f.jpg#00112233445566778899aabbccddeeff") == "aesgcm://h/f.jpg#***"


def test_deps_report_runs():
    text = deps.report()
    assert "Python" in text
    assert deps.gtk_version() >= (4, 0)
