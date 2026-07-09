#!/usr/bin/env python3
"""Pure-Python MCP server for the KiCAD MCP backend.

This module puts the MCP Python SDK (FastMCP) directly in front of the existing
``KiCADInterface.handle_command`` dispatcher and calls it **in-process** — no
Node/TypeScript layer, no subprocess spawn. The TypeScript ``server.tool(...)``
registrations were thin pass-throughs that shelled out to this same Python
backend; here we skip that hop entirely.

Tool-exposure strategy (see README / build log for the rationale):

1. A generic ``kicad_call(command, params)`` tool that covers the entire backend
   immediately (the long tail of ~193 commands).
2. Auto-registration of every command in ``KiCADInterface.command_routes`` as an
   individually named pass-through tool, with descriptions sourced from the
   backend's ``TOOL_SCHEMAS`` / IPC annotations. This mirrors the TS server's
   tool breadth without hand-porting 220 registrations.
3. A curated set of typed, first-class wrappers for high-value read/export verbs
   (registered first, so they take precedence over the generic auto-registered
   version of the same name).

Degradation note: ``pcbnew`` (SWIG) is supplied by a KiCad install and is not
pip-installable. File-based and IPC commands work standalone; SWIG-only board
commands only work when this server runs inside/alongside KiCad's Python. This
mirrors the Loom FileBridge philosophy.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

# Ensure the backend package root (the directory that contains ``kicad_interface``
# and its sibling top-level packages: commands/, schemas/, utils/, ...) is on
# sys.path. When installed from the wheel these already sit at the top level of
# site-packages, but in an editable / source checkout this parent is ``python/``.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from mcp.server.fastmcp import FastMCP  # noqa: E402

# Curated high-value tools that get typed, first-class wrappers. Everything else
# is auto-registered as a generic pass-through. These names must exist in the
# backend's command_routes.
CURATED_TOOLS = [
    "get_project_info",
    "get_board_info",
    "get_board_2d_view",
    "get_component_list",
    "get_nets_list",
    "get_layer_list",
    "get_design_rules",
    "run_drc",
    "run_erc",
    "export_gerber",
    "export_gerbers",
    "export_bom",
    "export_pdf",
    "export_svg",
    "generate_netlist",
    "list_schematic_components",
    "list_schematic_nets",
    "list_libraries",
    "get_backend_state",
]

_interface: Optional[Any] = None
_annotation_loader: Optional[Any] = None
_tool_schemas: Dict[str, Any] = {}


def get_interface() -> Any:
    """Lazily construct the single shared KiCADInterface.

    Instantiation is deferred so that simply importing this module (e.g. to list
    tools) does not pay the backend's startup cost or attempt an IPC connection.
    """
    global _interface
    if _interface is None:
        import kicad_interface  # noqa: WPS433 (deliberately deferred)

        _interface = kicad_interface.KiCADInterface()
    return _interface


def _load_metadata() -> None:
    """Load command descriptions from the backend's schema/annotation registries."""
    global _annotation_loader, _tool_schemas
    if _annotation_loader is not None:
        return
    try:
        from annotations import AnnotationLoader
        from schemas.tool_schemas import TOOL_SCHEMAS

        _annotation_loader = AnnotationLoader()
        _tool_schemas = TOOL_SCHEMAS
    except Exception:  # pragma: no cover - metadata is best-effort
        _annotation_loader = False  # sentinel: attempted, unavailable
        _tool_schemas = {}


def _describe(command: str) -> str:
    """Best-effort human description for a backend command."""
    _load_metadata()
    schema = _tool_schemas.get(command) if _tool_schemas else None
    if schema and schema.get("description"):
        return str(schema["description"])
    if _annotation_loader:
        desc = _annotation_loader.description(command)
        if desc:
            return str(desc)
    return f"KiCAD backend command: {command} (pass-through to handle_command)."


def _enumerate_commands() -> list[str]:
    """Return every command the backend can dispatch.

    Enumerated from a real KiCADInterface instance's ``command_routes`` so the
    exposed tool set always tracks the backend, with no hand-maintained list.
    """
    return sorted(get_interface().command_routes.keys())


def _make_passthrough(command: str):
    """Build a named pass-through tool function for a single backend command."""

    def _tool(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return get_interface().handle_command(command, params or {})

    _tool.__name__ = command
    _tool.__doc__ = _describe(command)
    return _tool


def build_server() -> FastMCP:
    """Construct and populate the FastMCP server (in-process, no subprocess)."""
    mcp = FastMCP("kicad-mcp-server")

    # --- generic escape hatch: covers the entire backend immediately ---
    @mcp.tool(
        name="kicad_call",
        description=(
            "Generic pass-through to the KiCAD backend. Dispatches `command` with "
            "`params` via KiCADInterface.handle_command() in-process. Use this for "
            "any backend command, including those without a dedicated typed tool."
        ),
    )
    def kicad_call(command: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return get_interface().handle_command(command, params or {})

    registered = {"kicad_call"}

    # --- curated, first-class typed wrappers (take precedence) ---
    for name in CURATED_TOOLS:
        if name in registered:
            continue
        mcp.add_tool(_make_passthrough(name), name=name, description=_describe(name))
        registered.add(name)

    # --- auto-register the full command set (generated breadth) ---
    for command in _enumerate_commands():
        if command in registered:
            continue
        mcp.add_tool(_make_passthrough(command), name=command, description=_describe(command))
        registered.add(command)

    return mcp


def main() -> None:
    """Console entry point: run the FastMCP server over stdio."""
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
