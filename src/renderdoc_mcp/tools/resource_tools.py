"""Resource analysis tools: list_textures, list_buffers, list_resources, get_resource_usage."""

from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import MCPServer

from renderdoc_mcp.session import get_session
from renderdoc_mcp.util import (
    rd,
    to_json,
    make_error,
    serialize_texture_desc,
    serialize_buffer_desc,
    serialize_usage_entry,
)


def register(mcp: MCPServer):
    @mcp.tool()
    def list_textures(filter_format: Optional[str] = None, min_width: Optional[int] = None) -> str:
        """List all textures in the capture.

        Args:
            filter_format: Optional format substring to filter by (e.g. "R8G8B8A8", "BC7").
            min_width: Optional minimum width to filter by.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        textures = session.controller.GetTextures()
        results = []
        for tex in textures:
            if min_width is not None and tex.width < min_width:
                continue
            fmt_name = str(tex.format.Name())
            if filter_format and filter_format.upper() not in fmt_name.upper():
                continue
            results.append(serialize_texture_desc(tex))

        return to_json({"textures": results, "count": len(results)})

    @mcp.tool()
    def list_buffers(min_size: Optional[int] = None) -> str:
        """List all buffers in the capture.

        Args:
            min_size: Optional minimum byte size to filter by.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        buffers = session.controller.GetBuffers()
        results = []
        for buf in buffers:
            if min_size is not None and buf.length < min_size:
                continue
            results.append(serialize_buffer_desc(buf))

        return to_json({"buffers": results, "count": len(results)})

    @mcp.tool()
    def list_resources(type_filter: Optional[str] = None, name_pattern: Optional[str] = None) -> str:
        """List all named resources in the capture.

        Args:
            type_filter: Optional resource type substring filter (e.g. "Texture", "Buffer").
            name_pattern: Optional regex pattern to filter resource names (case-insensitive).
        """
        import re

        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        resources = session.controller.GetResources()
        pattern = None
        if name_pattern:
            try:
                pattern = re.compile(name_pattern, re.IGNORECASE)
            except re.error as e:
                return to_json(make_error(f"Invalid regex pattern: {e}", "API_ERROR"))
        results = []

        for res in resources:
            if type_filter and type_filter.lower() not in str(res.type).lower():
                continue
            try:
                name = res.name
            except Exception:
                name = f"<unreadable name for {str(res.resourceId)}>"
            if not isinstance(name, str):
                try:
                    name = name.decode("utf-8", errors="replace")
                except Exception:
                    name = repr(name)
            if pattern and not pattern.search(name):
                continue
            results.append({
                "resource_id": str(res.resourceId),
                "name": name,
                "type": str(res.type),
            })

        return to_json({"resources": results, "count": len(results)})

    @mcp.tool()
    def get_resource_usage(resource_id: str) -> str:
        """Get the usage history of a resource across all events.

        Shows which events read from or write to this resource.

        Args:
            resource_id: The resource ID string (as returned by other tools).
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        rid = session.resolve_resource_id(resource_id)
        if rid is None:
            return to_json(make_error(f"Resource ID '{resource_id}' not found", "INVALID_RESOURCE_ID"))

        usages = session.controller.GetUsage(rid)
        results = [serialize_usage_entry(u) for u in usages]

        return to_json({"resource_id": resource_id, "usages": results, "count": len(results)})
