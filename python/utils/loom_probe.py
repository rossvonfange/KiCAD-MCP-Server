"""Probe for the Loom/InferSynth ``loom.capability`` engine.

The KiCAD MCP server must stay engine-agnostic: this module is the *only*
seam through which the server ever calls into Loom. It never imports
``loom.*`` logic into the MCP itself — it only (a) detects whether
``loom.capability`` is reachable, and (b) if so, calls into it either
in-process or via a configured interpreter subprocess.

Detection order:
    1. ``import loom.capability`` in the MCP's own interpreter.
    2. The interpreter named by the ``LOOM_PYTHON`` env var (default: the
       Loom repo's own venv), probed via a JSON round-trip subprocess.
    3. Neither works -> ``{"available": False, "reason": ..., "how_to_install": ...}``.

Every public function here is designed to **never raise** to its caller —
callers (MCP tool handlers) should be able to trust that a failure to reach
the engine comes back as a normal dict, not an exception, so the public MCP
keeps working when Loom isn't installed.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kicad_interface")

# Default location of the Loom engine's own venv interpreter. Overridable via
# the LOOM_PYTHON environment variable for other machines/layouts.
DEFAULT_LOOM_PYTHON = "/home/cycix/Desktop/fai-tuner/Loom/.venv/bin/python"

_SUBPROCESS_TIMEOUT_SECONDS = 20

_HOW_TO_INSTALL = (
    "loom.capability was not importable in-process, and the configured "
    "LOOM_PYTHON interpreter does not have it either. To fix: (1) install "
    "the Loom engine into this MCP server's own Python environment, or "
    "(2) point the LOOM_PYTHON environment variable at a Python interpreter "
    "that has `loom` installed, e.g. "
    "`export LOOM_PYTHON=/path/to/Loom/.venv/bin/python`."
)


def resolve_loom_python() -> str:
    """The interpreter path the subprocess seam will try, env override first."""
    return os.environ.get("LOOM_PYTHON") or DEFAULT_LOOM_PYTHON


def _in_process_probe() -> Optional[Dict[str, Any]]:
    """Try importing loom.capability in this process. None if unavailable."""
    try:
        module = importlib.import_module("loom.capability")
    except Exception as exc:  # noqa: BLE001 - any import failure means "absent"
        logger.debug("in-process loom.capability import failed: %s", exc)
        return None
    return {
        "available": True,
        "mode": "in-process",
        "interpreter": sys.executable,
        "version": getattr(module, "__version__", None),
        "reason": None,
        "how_to_install": None,
    }


_PROBE_SUBPROCESS_SOURCE = (
    "import importlib, json, sys\n"
    "try:\n"
    "    module = importlib.import_module('loom.capability')\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'available': False, 'error': f'{type(exc).__name__}: {exc}'}))\n"
    "    sys.exit(0)\n"
    "print(json.dumps({'available': True, 'version': getattr(module, '__version__', None)}))\n"
)


def _subprocess_probe(interpreter: str) -> Optional[Dict[str, Any]]:
    """Round-trip JSON probe of loom.capability importability via `interpreter`.

    Returns None (not an exception) for any failure mode: missing interpreter,
    timeout, non-zero exit, unparsable output, or a reported import error.
    """
    if not interpreter or not os.path.isfile(interpreter):
        logger.debug("LOOM_PYTHON interpreter not found at %s", interpreter)
        return None
    try:
        proc = subprocess.run(
            [interpreter, "-c", _PROBE_SUBPROCESS_SOURCE],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("loom probe subprocess errored: %s", exc)
        return None

    if proc.returncode != 0:
        logger.debug(
            "loom probe subprocess exited %s: stderr=%r", proc.returncode, proc.stderr
        )
        return None

    try:
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        payload = json.loads(lines[-1]) if lines else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("loom probe subprocess produced unparsable output: %s", exc)
        return None

    if not payload.get("available"):
        logger.debug("loom probe subprocess reports unavailable: %s", payload.get("error"))
        return None

    return {
        "available": True,
        "mode": "subprocess",
        "interpreter": interpreter,
        "version": payload.get("version"),
        "reason": None,
        "how_to_install": None,
    }


def probe_loom() -> Dict[str, Any]:
    """Detect whether ``loom.capability`` is usable, never raising.

    Returns a dict always containing at least ``available`` (bool). When
    available: ``mode`` ("in-process" | "subprocess"), ``interpreter``,
    ``version``. When unavailable: ``reason`` and ``how_to_install`` describing
    what to do next.
    """
    try:
        result = _in_process_probe()
        if result:
            return result
    except Exception as exc:  # noqa: BLE001 - defensive; _in_process_probe already guards
        logger.warning("unexpected error during in-process loom probe: %s", exc)

    interpreter = resolve_loom_python()
    try:
        result = _subprocess_probe(interpreter)
        if result:
            return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("unexpected error during subprocess loom probe: %s", exc)

    return {
        "available": False,
        "mode": None,
        "interpreter": interpreter,
        "version": None,
        "reason": (
            "loom.capability is not importable in-process, and the configured "
            f"LOOM_PYTHON interpreter ({interpreter}) does not have it either."
        ),
        "how_to_install": _HOW_TO_INSTALL,
    }


# ---------------------------------------------------------------------------
# recognize_region seam
# ---------------------------------------------------------------------------

_RECOGNIZE_REGION_SUBPROCESS_SOURCE = (
    "import dataclasses, json, sys\n"
    "def _to_jsonable(obj):\n"
    "    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):\n"
    "        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}\n"
    "    if isinstance(obj, (list, tuple, set, frozenset)):\n"
    "        return [_to_jsonable(v) for v in obj]\n"
    "    if isinstance(obj, dict):\n"
    "        return {k: _to_jsonable(v) for k, v in obj.items()}\n"
    "    return obj\n"
    "\n"
    "payload = json.loads(sys.stdin.read())\n"
    "pcb_path = payload['pcb_path']\n"
    "refs = payload.get('refs')\n"
    "outline = payload.get('outline')\n"
    "\n"
    "try:\n"
    "    from loom.capability import FileBridge, recognize_region\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'success': False, 'error': f'import failed: {type(exc).__name__}: {exc}'}))\n"
    "    sys.exit(0)\n"
    "\n"
    "try:\n"
    "    bridge = FileBridge(pcb_path)\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'success': False, 'error': f'FileBridge init failed: {type(exc).__name__}: {exc}'}))\n"
    "    sys.exit(0)\n"
    "\n"
    "catalog = None\n"
    "try:\n"
    "    from loom.capability import default_catalog\n"
    "    catalog = default_catalog()\n"
    "except Exception:\n"
    "    catalog = None\n"
    "\n"
    "kwargs = {}\n"
    "if refs is not None:\n"
    "    kwargs['refs'] = refs\n"
    "if outline is not None:\n"
    "    kwargs['outline'] = tuple(outline)\n"
    "if catalog is not None:\n"
    "    kwargs['catalog'] = catalog\n"
    "\n"
    "try:\n"
    "    result = recognize_region(bridge, **kwargs)\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'success': False, 'error': f'{type(exc).__name__}: {exc}'}))\n"
    "    sys.exit(0)\n"
    "\n"
    "print(json.dumps({'success': True, 'result': _to_jsonable(result)}))\n"
)


def _to_jsonable(obj: Any) -> Any:
    """Best-effort conversion of dataclasses/tuples/sets to JSON-safe values."""
    import dataclasses

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


def _recognize_region_in_process(
    pcb_path: str, refs: Optional[List[str]], outline: Optional[List[float]]
) -> Dict[str, Any]:
    try:
        from loom.capability import FileBridge, recognize_region  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"import failed: {type(exc).__name__}: {exc}"}

    try:
        bridge = FileBridge(pcb_path)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": f"FileBridge init failed: {type(exc).__name__}: {exc}",
        }

    catalog = None
    try:
        from loom.capability import default_catalog  # type: ignore

        catalog = default_catalog()
    except Exception:  # noqa: BLE001
        catalog = None

    kwargs: Dict[str, Any] = {}
    if refs is not None:
        kwargs["refs"] = refs
    if outline is not None:
        kwargs["outline"] = tuple(outline)
    if catalog is not None:
        kwargs["catalog"] = catalog

    try:
        result = recognize_region(bridge, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    return {"success": True, "result": _to_jsonable(result)}


def _recognize_region_subprocess(
    interpreter: str,
    pcb_path: str,
    refs: Optional[List[str]],
    outline: Optional[List[float]],
) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            [interpreter, "-c", _RECOGNIZE_REGION_SUBPROCESS_SOURCE],
            input=json.dumps({"pcb_path": pcb_path, "refs": refs, "outline": outline}),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"subprocess call failed: {type(exc).__name__}: {exc}"}

    if proc.returncode != 0:
        return {
            "success": False,
            "error": f"subprocess exited {proc.returncode}: {(proc.stderr or '').strip()[:2000]}",
        }

    try:
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else {"success": False, "error": "no output"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"unparsable subprocess output: {exc}"}


def run_recognize_region(
    pcb_path: str,
    refs: Optional[List[str]] = None,
    outline: Optional[List[float]] = None,
    probe: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call ``loom.capability.recognize_region`` via whichever seam is available.

    Never raises. Returns ``{"success": True, "result": {...}}`` on success or
    ``{"success": False, "error": ..., "how_to_install": ...}`` when the engine
    is unavailable or the call itself fails.
    """
    probe = probe if probe is not None else probe_loom()
    if not probe.get("available"):
        return {
            "success": False,
            "error": probe.get("reason", "loom.capability unavailable"),
            "how_to_install": probe.get("how_to_install"),
        }

    if probe.get("mode") == "in-process":
        return _recognize_region_in_process(pcb_path, refs, outline)

    interpreter = probe.get("interpreter") or resolve_loom_python()
    return _recognize_region_subprocess(interpreter, pcb_path, refs, outline)
