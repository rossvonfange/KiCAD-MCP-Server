"""Pure-Python (Node-free) MCP server for the KiCAD MCP backend."""

from .server import build_server, get_interface, main

__all__ = ["build_server", "get_interface", "main"]
