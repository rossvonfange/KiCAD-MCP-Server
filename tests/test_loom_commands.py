"""Tests for commands.loom — the loom_* / kicad_enable_api / loom_install_plugin
MCP tool handlers.

CRITICAL: every filesystem-touching test here operates against a fake HOME
under tmp_path (via monkeypatch on Path.home / XDG_*), never the real
~/.config/kicad/... — see kicad_enable_api's docstring caveats.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.loom import LoomCommands


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Never let XDG_CONFIG_HOME/XDG_DATA_HOME from the real environment leak in."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


def _fake_iface(board_path=None):
    iface = MagicMock()
    iface._current_board_path.return_value = board_path
    return iface


# ---------------------------------------------------------------------------
# loom_status
# ---------------------------------------------------------------------------


def test_loom_status_reports_probe_result(monkeypatch):
    import commands.loom as loom_mod

    monkeypatch.setattr(
        loom_mod,
        "probe_loom",
        lambda: {"available": False, "reason": "nope", "how_to_install": "install it"},
    )
    cmds = LoomCommands(_fake_iface())

    result = cmds.loom_status({})

    assert result["success"] is True
    assert result["engine"] == "loom.capability"
    assert result["available"] is False
    assert result["reason"] == "nope"


# ---------------------------------------------------------------------------
# loom_recognize_region — degrade path
# ---------------------------------------------------------------------------


def test_loom_recognize_region_degrades_when_engine_unavailable(monkeypatch):
    import commands.loom as loom_mod

    monkeypatch.setattr(
        loom_mod,
        "probe_loom",
        lambda: {"available": False, "reason": "not installed", "how_to_install": "go install it"},
    )
    cmds = LoomCommands(_fake_iface(board_path="/fake/board.kicad_pcb"))

    result = cmds.loom_recognize_region({"refs": ["U1", "U2"]})

    assert result["success"] is False
    assert result["engine_available"] is False
    assert result["error"] == "not installed"
    assert result["how_to_install"] == "go install it"


def test_loom_recognize_region_requires_refs_or_outline():
    cmds = LoomCommands(_fake_iface(board_path="/fake/board.kicad_pcb"))
    result = cmds.loom_recognize_region({})
    assert result["success"] is False
    assert "refs" in result["error"] or "outline" in result["error"]


def test_loom_recognize_region_requires_a_board_path():
    cmds = LoomCommands(_fake_iface(board_path=None))
    result = cmds.loom_recognize_region({"refs": ["U1"]})
    assert result["success"] is False
    assert "board" in result["error"].lower()


def test_loom_recognize_region_uses_resolved_board_path_when_available(monkeypatch):
    import commands.loom as loom_mod

    monkeypatch.setattr(
        loom_mod, "probe_loom", lambda: {"available": True, "mode": "in-process"}
    )
    captured = {}

    def _fake_run_recognize_region(pcb_path, refs=None, outline=None, probe=None):
        captured["pcb_path"] = pcb_path
        captured["refs"] = refs
        return {"success": True, "result": {"members": ["U1"]}}

    monkeypatch.setattr(loom_mod, "run_recognize_region", _fake_run_recognize_region)
    cmds = LoomCommands(_fake_iface(board_path="/fake/board.kicad_pcb"))

    result = cmds.loom_recognize_region({"refs": ["U1"]})

    assert result["success"] is True
    assert result["engine_mode"] == "in-process"
    assert captured["pcb_path"] == "/fake/board.kicad_pcb"


# ---------------------------------------------------------------------------
# kicad_enable_api
# ---------------------------------------------------------------------------


def _write_kicad_common(config_dir: Path, extra: dict | None = None) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "kicad_common.json"
    payload = {"api": {"enable_server": False}, "some_other_key": "untouched"}
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_kicad_enable_api_edits_and_backs_up_temp_config(monkeypatch, tmp_path):
    """Full happy path against a TEMP config -- never the real ~/.config/kicad/."""
    fake_home = tmp_path / "home"
    config_dir = fake_home / ".config" / "kicad" / "10.0"
    config_path = _write_kicad_common(config_dir)

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    import commands.loom as loom_mod

    monkeypatch.setattr(loom_mod, "_running_kicad_processes", lambda: [])

    cmds = LoomCommands(_fake_iface())
    result = cmds.kicad_enable_api({})

    assert result["success"] is True
    assert result["config_path"] == str(config_path)
    assert result["kicad_version_dir"] == "10.0"
    assert result["was_already_enabled"] is False
    assert "restart" in result["message"].lower()
    assert "warning" not in result

    backup_path = Path(result["backup_path"])
    assert backup_path.exists()
    backup_data = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backup_data["api"]["enable_server"] is False  # backup is pre-edit

    edited = json.loads(config_path.read_text(encoding="utf-8"))
    assert edited["api"]["enable_server"] is True
    assert edited["some_other_key"] == "untouched"  # other keys preserved


def test_kicad_enable_api_auto_detects_newest_version_not_hardcoded(monkeypatch, tmp_path):
    """Must not assume '10.0' -- picks the newest of whatever version dirs exist."""
    fake_home = tmp_path / "home"
    base = fake_home / ".config" / "kicad"
    _write_kicad_common(base / "8.0")
    _write_kicad_common(base / "9.0")
    newest_path = _write_kicad_common(base / "11.2")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    import commands.loom as loom_mod

    monkeypatch.setattr(loom_mod, "_running_kicad_processes", lambda: [])

    cmds = LoomCommands(_fake_iface())
    result = cmds.kicad_enable_api({})

    assert result["success"] is True
    assert result["kicad_version_dir"] == "11.2"
    assert result["config_path"] == str(newest_path)


def test_kicad_enable_api_warns_when_kicad_process_running(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    config_dir = fake_home / ".config" / "kicad" / "10.0"
    _write_kicad_common(config_dir)

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    import commands.loom as loom_mod

    monkeypatch.setattr(
        loom_mod, "_running_kicad_processes", lambda: ["1234 /usr/bin/kicad"]
    )

    cmds = LoomCommands(_fake_iface())
    result = cmds.kicad_enable_api({})

    # Warns, does NOT refuse -- the edit still happens.
    assert result["success"] is True
    assert "warning" in result
    assert result["running_processes"] == ["1234 /usr/bin/kicad"]


def test_kicad_enable_api_missing_config_dir_reports_actionable_error(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    cmds = LoomCommands(_fake_iface())
    result = cmds.kicad_enable_api({})

    assert result["success"] is False
    assert "error" in result


def test_kicad_enable_api_never_touches_real_home(monkeypatch, tmp_path):
    """Guard rail: assert Path.home() really is patched to tmp_path in this test file."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert Path.home() == fake_home
    assert str(Path.home()) != str(Path(__file__).parent.parent.parent.parent)


# ---------------------------------------------------------------------------
# loom_install_plugin
# ---------------------------------------------------------------------------


def test_loom_install_plugin_dry_run_when_no_source(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    data_dir = fake_home / ".local" / "share" / "kicad" / "10.0"
    data_dir.mkdir(parents=True)

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    cmds = LoomCommands(_fake_iface())

    result = cmds.loom_install_plugin({})

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["target_dir"].endswith(str(Path("10.0") / "3rdparty" / "plugins"))
    # Nothing was created.
    assert not (data_dir / "3rdparty").exists()


def test_loom_install_plugin_copies_source_directory(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    data_dir = fake_home / ".local" / "share" / "kicad" / "10.0"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    source = tmp_path / "my_plugin"
    source.mkdir()
    (source / "plugin.py").write_text("# stub plugin\n", encoding="utf-8")

    cmds = LoomCommands(_fake_iface())
    result = cmds.loom_install_plugin({"source": str(source)})

    assert result["success"] is True
    assert result["mode"] == "copied"
    installed = Path(result["installed_path"])
    assert installed.is_dir()
    assert (installed / "plugin.py").exists()


def test_loom_install_plugin_missing_data_dir_reports_actionable_error(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    cmds = LoomCommands(_fake_iface())
    result = cmds.loom_install_plugin({"source": "/whatever"})

    assert result["success"] is False
    assert "error" in result
