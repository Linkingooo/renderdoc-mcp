"""Session management tools: open_capture, close_capture, get_capture_info, get_frame_overview."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from renderdoc_mcp.session import get_session
from renderdoc_mcp.util import rd, to_json, make_error, flags_to_list


def _get_gpu_quirks(driver_name: str) -> list[str]:
    """Return known GPU/API quirks based on the driver name string."""
    quirks: list[str] = []
    name_upper = driver_name.upper()

    if "ADRENO" in name_upper:
        quirks.extend([
            "Adreno: mediump float 实际精度可能低于 spec 要求，关键计算建议强制 highp",
            "Adreno: R11G11B10_FLOAT 没有符号位，写入负值会被 clamp 到 0 或产生未定义行为",
            "Adreno: 某些驱动版本 textureLod 在 fragment shader 中存在 bug",
        ])
    elif "MALI" in name_upper:
        quirks.extend([
            "Mali: R11G11B10_FLOAT 存储负值行为未定义",
            "Mali: 大量 discard 指令会导致 early-Z 失效，影响 tile-based 性能",
            "Mali: 某些型号 mediump 向量运算累积精度问题",
        ])
    elif "POWERVR" in name_upper or "IMAGINATION" in name_upper:
        quirks.extend([
            "PowerVR: Tile-based deferred rendering，避免不必要的 framebuffer load/store",
            "PowerVR: 避免大量纹理采样器绑定",
        ])
    elif "APPLE" in name_upper:
        quirks.extend([
            "Apple GPU: High-efficiency TBDR，注意 tile memory 带宽",
            "Apple: float16 在 Metal 上性能优秀，建议用于后处理",
        ])

    if "OPENGL ES" in name_upper or "GLES" in name_upper:
        quirks.append("OpenGL ES: 注意精度限定符 (highp/mediump/lowp) 的正确使用")
        quirks.append("OpenGL ES: 扩展依赖需检查 GL_EXTENSIONS 兼容性")

    return quirks


def register(mcp: FastMCP):
    @mcp.tool()
    def open_capture(filepath: str, remote_host: str | None = None) -> str:
        """Open a RenderDoc capture (.rdc) file for analysis.

        Automatically closes any previously opened capture.
        Returns capture overview: API type, action count, resource counts.

        Args:
            filepath: Absolute path to the .rdc capture file.
            remote_host: Optional RenderDoc remote server address
                (e.g. "127.0.0.1:39920") to replay on instead of the local
                GPU. Use this when the capture needs hardware features the
                local GPU lacks, such as ETC2 texture compression on a
                capture made on a mobile device - point this at a RenderDoc
                remote server running on (or adb-forwarded from) that
                device.
        """
        filepath = os.path.normpath(filepath)
        session = get_session()
        result = session.open(filepath, remote_host=remote_host)
        return to_json(result)

    @mcp.tool()
    def close_capture() -> str:
        """Close the currently open capture file and free resources."""
        session = get_session()
        return to_json(session.close())

    @mcp.tool()
    def get_capture_info() -> str:
        """Get information about the currently open capture.

        Returns API type, file path, action count, texture/buffer counts,
        and known GPU quirks based on the detected driver/GPU.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        controller = session.controller
        textures = controller.GetTextures()
        buffers = controller.GetBuffers()

        driver_name = session.driver_name

        # Detect resolution from most-used render target
        rt_draw_counts: dict[str, int] = {}
        for _eid, action in session.action_map.items():
            if action.flags & rd.ActionFlags.Drawcall:
                for o in action.outputs:
                    if int(o) != 0:
                        key = str(o)
                        rt_draw_counts[key] = rt_draw_counts.get(key, 0) + 1
        resolution = "unknown"
        main_color_format = "unknown"
        if rt_draw_counts:
            top_rid = max(rt_draw_counts, key=lambda k: rt_draw_counts[k])
            tex_desc = session.get_texture_desc(top_rid)
            if tex_desc is not None:
                resolution = f"{tex_desc.width}x{tex_desc.height}"
                main_color_format = str(tex_desc.format.Name())

        info = {
            "filepath": session.filepath,
            "api": driver_name,
            "remote_host": session.remote_host,
            "resolution": resolution,
            "main_color_format": main_color_format,
            "total_actions": len(session.action_map),
            "textures": len(textures),
            "buffers": len(buffers),
            "current_event": session.current_event,
            "known_gpu_quirks": _get_gpu_quirks(driver_name),
        }
        return to_json(info)

    @mcp.tool()
    def get_frame_overview() -> str:
        """Get a frame-level statistics overview of the current capture.

        Returns action counts by type, texture/buffer memory totals,
        main render targets with draw counts, and estimated resolution.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        controller = session.controller

        # Count actions by type
        draw_calls = 0
        clears = 0
        dispatches = 0
        for _eid, action in session.action_map.items():
            if action.flags & rd.ActionFlags.Drawcall:
                draw_calls += 1
            if action.flags & rd.ActionFlags.Clear:
                clears += 1
            if action.flags & rd.ActionFlags.Dispatch:
                dispatches += 1

        # Texture memory
        textures = controller.GetTextures()
        tex_total_bytes = 0
        for tex in textures:
            # Estimate: width * height * depth * arraysize * bpp (assume 4 bytes as fallback)
            bpp = 4
            fmt_name = str(tex.format.Name()).upper()
            if "R16G16B16A16" in fmt_name:
                bpp = 8
            elif "R32G32B32A32" in fmt_name:
                bpp = 16
            elif "R8G8B8A8" in fmt_name or "B8G8R8A8" in fmt_name:
                bpp = 4
            elif "R16G16" in fmt_name:
                bpp = 4
            elif "R32" in fmt_name and "G32" not in fmt_name:
                bpp = 4
            elif "R16" in fmt_name and "G16" not in fmt_name:
                bpp = 2
            elif "R8" in fmt_name and "G8" not in fmt_name:
                bpp = 1
            elif "BC" in fmt_name or "ETC" in fmt_name or "ASTC" in fmt_name:
                bpp = 1  # compressed: ~1 byte per pixel average
            elif "D24" in fmt_name or "D32" in fmt_name:
                bpp = 4
            elif "D16" in fmt_name:
                bpp = 2
            size = tex.width * tex.height * max(tex.depth, 1) * max(tex.arraysize, 1) * bpp
            # Account for mip chain (~1.33x)
            if tex.mips > 1:
                size = int(size * 1.33)
            tex_total_bytes += size

        # Buffer memory
        buffers = controller.GetBuffers()
        buf_total_bytes = sum(buf.length for buf in buffers)

        # Detect render targets and their draw counts
        rt_draw_counts: dict[str, int] = {}
        for _eid, action in sorted(session.action_map.items()):
            if not (action.flags & rd.ActionFlags.Drawcall):
                continue
            for o in action.outputs:
                if int(o) != 0:
                    key = str(o)
                    rt_draw_counts[key] = rt_draw_counts.get(key, 0) + 1

        render_targets = []
        for rid_str, count in sorted(rt_draw_counts.items(), key=lambda x: -x[1]):
            rt_entry: dict = {"resource_id": rid_str, "draw_count": count}
            tex_desc = session.get_texture_desc(rid_str)
            if tex_desc is not None:
                rt_entry["size"] = f"{tex_desc.width}x{tex_desc.height}"
                rt_entry["format"] = str(tex_desc.format.Name())
            render_targets.append(rt_entry)

        # Infer resolution from the most-used render target
        resolution = "unknown"
        if render_targets:
            top_rt = render_targets[0]
            if "size" in top_rt:
                resolution = top_rt["size"]

        result = {
            "filepath": session.filepath,
            "api": session.driver_name,
            "resolution": resolution,
            "total_actions": len(session.action_map),
            "draw_calls": draw_calls,
            "clears": clears,
            "dispatches": dispatches,
            "textures": {
                "count": len(textures),
                "total_memory_mb": round(tex_total_bytes / (1024 * 1024), 2),
            },
            "buffers": {
                "count": len(buffers),
                "total_memory_mb": round(buf_total_bytes / (1024 * 1024), 2),
            },
            "render_targets": render_targets,
        }
        return to_json(result)
