"""Operational nice-to-haves for the Loom/InferSynth engine + KiCad environment.

Wires the ``loom_probe`` seam into MCP tool handlers (``loom_status``,
``loom_recognize_region``) and adds two KiCad environment-bootstrap tools
(``kicad_enable_api``, ``loom_install_plugin``) for chores the team was
previously doing by hand.

Boundary: this module never imports Loom/InferSynth logic directly — every
engine call goes through ``utils.loom_probe``, which is the only seam that
reaches into ``loom.capability`` (in-process or via a configured
interpreter subprocess). That keeps the public MCP usable, with a friendly
degrade message, even when the private engine isn't installed.
"""

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.loom_probe import (
    probe_loom,
    run_explain_board,
    run_lift_to_frd,
    run_plan_io,
    run_recognize_region,
    run_synthesize_fabric,
)

logger = logging.getLogger("kicad_interface")

_VERSION_DIR_RE = re.compile(r"^\d+(\.\d+)*$")


def _version_key(name: str):
    """Sort key for version-like dir names ('10.0' > '9.0' > '8.0')."""
    key = []
    for part in name.split("."):
        try:
            key.append(int(part))
        except ValueError:
            key.append(0)
    return tuple(key)


def _kicad_config_bases() -> List[Path]:
    """Platform-appropriate KiCad *config* base dirs (parent of the <ver> dirs).

    Deliberately does NOT hardcode a version — callers glob the children.
    """
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return [home / "Library" / "Preferences" / "kicad"]
    if system == "Windows":
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        return [appdata / "kicad"]
    bases: List[Path] = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        bases.append(Path(xdg) / "kicad")
    bases.append(home / ".config" / "kicad")
    return bases


def _kicad_data_bases() -> List[Path]:
    """Platform-appropriate KiCad *data* base dirs (holds 3rdparty/)."""
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return [home / "Library" / "Application Support" / "kicad"]
    if system == "Windows":
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        return [appdata / "kicad"]
    bases: List[Path] = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        bases.append(Path(xdg) / "kicad")
    bases.append(home / ".local" / "share" / "kicad")
    return bases


def _discover_version_dirs(base_dirs: List[Path]) -> List[Path]:
    """Version-named subdirectories (e.g. '10.0', '9.0') under any base, newest first."""
    found: List[Path] = []
    for base in base_dirs:
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and _VERSION_DIR_RE.match(child.name):
                found.append(child)
    found.sort(key=lambda p: _version_key(p.name), reverse=True)
    return found


def _running_kicad_processes() -> List[str]:
    """Best-effort list of running KiCad-related process command lines.

    Uses ``ps``/``tasklist`` (no psutil dependency assumed). Returns [] on any
    failure — this only ever adds a *warning*, never blocks the edit, so a
    missed detection here is not unsafe, just less helpful.
    """
    try:
        if platform.system() == "Windows":
            proc = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=5)
            return [
                line
                for line in (proc.stdout or "").splitlines()
                if re.search(r"kicad|pcbnew|eeschema", line, re.IGNORECASE)
            ]
        proc = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=5)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not enumerate running processes: %s", exc)
        return []

    matches = []
    for line in (proc.stdout or "").splitlines():
        if "grep" in line.lower():
            continue
        if re.search(r"(bin/)?(kicad|pcbnew|eeschema)\b", line, re.IGNORECASE):
            matches.append(line.strip())
    return matches


class LoomCommands:
    """Handlers for the loom_* and kicad_enable_api MCP tools."""

    def __init__(self, iface):
        # Back-reference to the KiCadInterface instance, mirroring
        # SchematicHierarchyCommands(iface) — used only to resolve the
        # currently-open board's file path via the interface's existing
        # board-state plumbing (_current_board_path).
        self.iface = iface

    # -- loom_status ----------------------------------------------------------

    def loom_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Report Loom/InferSynth engine availability + resolved interpreter."""
        probe = probe_loom()
        return {"success": True, "engine": "loom.capability", **probe}

    # -- loom_recognize_region --------------------------------------------------

    def loom_recognize_region(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Call loom.capability.recognize_region against the open board.

        Args: either `refs` (list of reference designators) or `outline`
        ([x, y, w, h]); optional `boardPath` to override the open board.
        Gated on the probe: returns a friendly degrade message (never a stack
        trace) when the engine isn't reachable.
        """
        outline = params.get("outline")
        refs = params.get("refs")

        pcb_path = params.get("boardPath") or self._resolve_board_path()
        if not pcb_path:
            return {
                "success": False,
                "error": (
                    "No board is open and no boardPath was given; open a project "
                    "first or pass boardPath explicitly."
                ),
            }

        if not refs and not outline:
            return {
                "success": False,
                "error": (
                    "Provide either `refs` (list of reference designators) or "
                    "`outline` ([x, y, w, h])."
                ),
            }

        probe = probe_loom()
        if not probe.get("available"):
            return {
                "success": False,
                "engine_available": False,
                "error": probe.get("reason"),
                "how_to_install": probe.get("how_to_install"),
            }

        result = run_recognize_region(pcb_path, refs=refs, outline=outline, probe=probe)
        result.setdefault("engine_available", True)
        result["engine_mode"] = probe.get("mode")
        return result

    # -- loom_lift_to_frd -------------------------------------------------------

    def loom_lift_to_frd(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Lift the open board back into an observed FRD (Loom's `lift_to_frd`).

        Optional `boardPath` overrides the currently open board. Gated on the
        probe: returns a friendly degrade message (never a stack trace) when
        the engine isn't reachable. On success returns `{markdown, requirements,
        gaps}`.
        """
        pcb_path = params.get("boardPath") or self._resolve_board_path()
        if not pcb_path:
            return {
                "success": False,
                "error": (
                    "No board is open and no boardPath was given; open a project "
                    "first or pass boardPath explicitly."
                ),
            }

        probe = probe_loom()
        if not probe.get("available"):
            return {
                "success": False,
                "engine_available": False,
                "error": probe.get("reason"),
                "how_to_install": probe.get("how_to_install"),
            }

        result = run_lift_to_frd(pcb_path, probe=probe)
        result.setdefault("engine_available", True)
        result["engine_mode"] = probe.get("mode")
        return result

    # -- loom_explain_board -------------------------------------------------------

    def loom_explain_board(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Render a human explanation of the open board (Loom's `explain_board`).

        Optional `boardPath` overrides the currently open board. Gated on the
        probe: returns a friendly degrade message (never a stack trace) when
        the engine isn't reachable.
        """
        pcb_path = params.get("boardPath") or self._resolve_board_path()
        if not pcb_path:
            return {
                "success": False,
                "error": (
                    "No board is open and no boardPath was given; open a project "
                    "first or pass boardPath explicitly."
                ),
            }

        probe = probe_loom()
        if not probe.get("available"):
            return {
                "success": False,
                "engine_available": False,
                "error": probe.get("reason"),
                "how_to_install": probe.get("how_to_install"),
            }

        result = run_explain_board(pcb_path, probe=probe)
        result.setdefault("engine_available", True)
        result["engine_mode"] = probe.get("mode")
        return result

    # -- loom_plan_io -------------------------------------------------------------

    def loom_plan_io(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Plan an IO pin assignment via Loom's `plan_io` CSP solver.

        Args: `device` (a device ref/name loadable from the Loom corpus store,
        e.g. an STM32 part number) and `interfaces` (list of requested
        peripheral instance names, e.g. ["SPI1", "USART2"]). Optional
        `allowedPins` (Class-B observed-pin restriction) and `consumedPins`
        (incremental-gate exhaustion). Gated on the probe: returns a friendly
        degrade message (never a stack trace) when the engine isn't reachable.
        """
        device = params.get("device")
        interfaces = params.get("interfaces")

        if not device:
            return {"success": False, "error": "Provide `device` (a device ref/name)."}
        if not interfaces:
            return {
                "success": False,
                "error": "Provide `interfaces` (a non-empty list of requested peripheral instance names).",
            }

        probe = probe_loom()
        if not probe.get("available"):
            return {
                "success": False,
                "engine_available": False,
                "error": probe.get("reason"),
                "how_to_install": probe.get("how_to_install"),
            }

        result = run_plan_io(
            device,
            interfaces,
            allowed_pins=params.get("allowedPins"),
            consumed_pins=params.get("consumedPins"),
            probe=probe,
        )
        result.setdefault("engine_available", True)
        result["engine_mode"] = probe.get("mode")
        return result

    # -- loom_synthesize_fabric ----------------------------------------------------

    def loom_synthesize_fabric(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Run two-gate fabric synthesis via Loom's `synthesize_fabric`.

        Args: `device` (a device ref/name loadable from the Loom corpus store)
        and `specText` (the compact stuff spec,
        `<region>:<TYPE|instance>[*count][@rot]`). Gated on the probe: returns
        a friendly degrade message (never a stack trace) when the engine isn't
        reachable. On success returns `{seated, contention}`.
        """
        device = params.get("device")
        spec_text = params.get("specText")

        if not device:
            return {"success": False, "error": "Provide `device` (a device ref/name)."}
        if not spec_text:
            return {
                "success": False,
                "error": "Provide `specText` (a compact stuff spec, e.g. 'region1:SPI1').",
            }

        probe = probe_loom()
        if not probe.get("available"):
            return {
                "success": False,
                "engine_available": False,
                "error": probe.get("reason"),
                "how_to_install": probe.get("how_to_install"),
            }

        result = run_synthesize_fabric(device, spec_text, probe=probe)
        result.setdefault("engine_available", True)
        result["engine_mode"] = probe.get("mode")
        return result

    def _resolve_board_path(self) -> Optional[str]:
        try:
            return self.iface._current_board_path()
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not resolve current board path: %s", exc)
            return None

    # -- kicad_enable_api -------------------------------------------------------

    def kicad_enable_api(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set api.enable_server=true in the user's kicad_common.json.

        Auto-detects the KiCad version config dir (never hardcodes a version),
        backs up the file before editing, and warns (does not refuse) if a
        KiCad process looks to be running, since KiCad overwrites this file
        with its in-memory settings on exit.
        """
        version_dirs = _discover_version_dirs(_kicad_config_bases())
        if not version_dirs:
            return {
                "success": False,
                "error": (
                    "No KiCad config directory found under "
                    f"{[str(b) for b in _kicad_config_bases()]}. KiCad must be run "
                    "at least once to create its settings directory."
                ),
            }

        version_override = params.get("version")
        if version_override:
            chosen = next((d for d in version_dirs if d.name == str(version_override)), None)
            if chosen is None:
                return {
                    "success": False,
                    "error": (
                        f"Requested version {version_override!r} not found; available: "
                        f"{[d.name for d in version_dirs]}"
                    ),
                }
        else:
            chosen = version_dirs[0]

        config_path = chosen / "kicad_common.json"
        if not config_path.is_file():
            return {
                "success": False,
                "error": f"{config_path} does not exist yet; run KiCad once first.",
                "detected_version_dirs": [d.name for d in version_dirs],
            }

        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"Failed to read/parse {config_path}: {exc}"}

        already_enabled = bool((data.get("api") or {}).get("enable_server"))

        backup_path = config_path.with_name(
            f"{config_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            shutil.copy2(config_path, backup_path)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"Failed to back up {config_path}: {exc}"}

        data.setdefault("api", {})
        data["api"]["enable_server"] = True

        try:
            config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error": f"Failed to write {config_path}: {exc}",
                "backup_path": str(backup_path),
            }

        result: Dict[str, Any] = {
            "success": True,
            "config_path": str(config_path),
            "backup_path": str(backup_path),
            "kicad_version_dir": chosen.name,
            "was_already_enabled": already_enabled,
            "message": (
                "api.enable_server set to true. Restart KiCad for this to take "
                "effect — the API server is only started at launch."
            ),
        }

        running = _running_kicad_processes()
        if running:
            result["warning"] = (
                "A KiCad process appears to be running. If it is still open, it "
                "may overwrite this file with its in-memory settings on exit and "
                "undo this change. Close KiCad, then relaunch it, to be safe."
            )
            result["running_processes"] = running[:10]

        return result

    # -- loom_install_plugin -----------------------------------------------------

    def loom_install_plugin(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Copy/symlink a plugin package into KiCad's 3rd-party plugin dir.

        Scaffolding only: does not fabricate a plugin. If `source` isn't given,
        reports where the plugin WOULD install and makes no changes.
        """
        version_dirs = _discover_version_dirs(_kicad_data_bases())
        if not version_dirs:
            return {
                "success": False,
                "error": (
                    "No KiCad data directory found under "
                    f"{[str(b) for b in _kicad_data_bases()]}. KiCad must be run "
                    "at least once to create its data directory."
                ),
            }

        version_override = params.get("version")
        if version_override:
            chosen = next((d for d in version_dirs if d.name == str(version_override)), None)
            if chosen is None:
                return {
                    "success": False,
                    "error": (
                        f"Requested version {version_override!r} not found; available: "
                        f"{[d.name for d in version_dirs]}"
                    ),
                }
        else:
            chosen = version_dirs[0]

        target_dir = chosen / "3rdparty" / "plugins"
        source = params.get("source")

        if not source:
            return {
                "success": True,
                "dry_run": True,
                "target_dir": str(target_dir),
                "message": (
                    "No `source` provided; nothing was installed. Pass `source` "
                    "(a directory or file for the plugin package) to install it "
                    f"under {target_dir}."
                ),
            }

        source_path = Path(source).expanduser()
        if not source_path.exists():
            return {"success": False, "error": f"source does not exist: {source_path}"}

        name = params.get("name") or source_path.name
        dest_path = target_dir / name

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"Failed to create {target_dir}: {exc}"}

        if dest_path.exists():
            return {
                "success": False,
                "error": f"{dest_path} already exists; remove it first or pass a different `name`.",
            }

        use_symlink = bool(params.get("link"))
        try:
            if use_symlink:
                dest_path.symlink_to(
                    source_path.resolve(), target_is_directory=source_path.is_dir()
                )
                mode = "symlinked"
            elif source_path.is_dir():
                shutil.copytree(source_path, dest_path)
                mode = "copied"
            else:
                shutil.copy2(source_path, dest_path)
                mode = "copied"
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"Failed to install plugin: {exc}"}

        return {
            "success": True,
            "target_dir": str(target_dir),
            "installed_path": str(dest_path),
            "mode": mode,
            "kicad_version_dir": chosen.name,
            "message": "Restart KiCad (or refresh the Plugin and Content Manager) to pick it up.",
        }
