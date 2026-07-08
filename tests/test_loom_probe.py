"""Tests for utils.loom_probe — the only seam through which the MCP calls into
the private Loom/InferSynth engine.

Covers the probe's degrade path (loom.capability not importable anywhere) and
that it never raises, plus that a working LOOM_PYTHON subprocess round-trip is
recognised as "available".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils import loom_probe


@pytest.fixture(autouse=True)
def _clear_loom_python_env(monkeypatch):
    monkeypatch.delenv("LOOM_PYTHON", raising=False)


def test_probe_degrades_when_loom_not_importable_and_no_interpreter(monkeypatch, tmp_path):
    """Neither in-process import nor a LOOM_PYTHON interpreter -> available=False, never raises."""
    monkeypatch.setattr(loom_probe, "_in_process_probe", lambda: None)
    # Point LOOM_PYTHON at a path that doesn't exist.
    missing_interpreter = str(tmp_path / "does-not-exist" / "python")
    monkeypatch.setenv("LOOM_PYTHON", missing_interpreter)

    result = loom_probe.probe_loom()

    assert result["available"] is False
    assert result["reason"]
    assert result["how_to_install"]
    assert result["interpreter"] == missing_interpreter


def test_probe_never_raises_when_in_process_probe_explodes(monkeypatch):
    """A bug in the in-process probe must not propagate to the caller."""

    def _boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(loom_probe, "_in_process_probe", _boom)
    monkeypatch.setattr(loom_probe, "_subprocess_probe", lambda interpreter: None)

    result = loom_probe.probe_loom()

    assert result["available"] is False


def test_probe_reports_in_process_when_loom_capability_importable(monkeypatch):
    """When loom.capability *is* importable in-process, the probe reports that seam."""
    import types

    fake_module = types.ModuleType("loom.capability")
    fake_module.__version__ = "0.0.1-test"

    monkeypatch.setattr(
        loom_probe,
        "_in_process_probe",
        lambda: {
            "available": True,
            "mode": "in-process",
            "interpreter": sys.executable,
            "version": "0.0.1-test",
            "reason": None,
            "how_to_install": None,
        },
    )

    result = loom_probe.probe_loom()

    assert result["available"] is True
    assert result["mode"] == "in-process"
    assert result["version"] == "0.0.1-test"


def test_probe_uses_real_subprocess_interpreter_round_trip():
    """A real subprocess round-trip against the current interpreter (which does
    NOT have loom.capability installed) must resolve to unavailable, not crash."""
    result = loom_probe.probe_loom()
    # Whatever LOOM_PYTHON resolves to in this environment, the probe must
    # always return a well-formed dict and never raise.
    assert "available" in result
    if not result["available"]:
        assert result["reason"]
        assert result["how_to_install"]


def test_subprocess_probe_success_with_fake_loom_package(tmp_path):
    """A LOOM_PYTHON interpreter that *does* have a `loom.capability` package
    importable must be detected as available via the subprocess seam."""
    fake_site = tmp_path / "fake_site"
    pkg_dir = fake_site / "loom"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "capability.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")

    fake_result = loom_probe._subprocess_probe(sys.executable)
    # Without PYTHONPATH pointing at fake_site, the real current interpreter
    # won't see the fake package -- confirm the negative case first.
    assert fake_result is None

    import os

    env_backup = os.environ.get("PYTHONPATH")
    try:
        os.environ["PYTHONPATH"] = str(fake_site) + (
            os.pathsep + env_backup if env_backup else ""
        )
        result = loom_probe._subprocess_probe(sys.executable)
    finally:
        if env_backup is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = env_backup

    assert result is not None
    assert result["available"] is True
    assert result["version"] == "9.9.9"


def test_run_recognize_region_degrades_cleanly_when_unavailable():
    probe = {
        "available": False,
        "reason": "not installed",
        "how_to_install": "install it",
    }
    result = loom_probe.run_recognize_region("/tmp/does-not-matter.kicad_pcb", probe=probe)
    assert result["success"] is False
    assert result["error"] == "not installed"
    assert result["how_to_install"] == "install it"
