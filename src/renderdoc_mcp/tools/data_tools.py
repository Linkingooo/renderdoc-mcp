"""Data extraction tools: save_texture, get_buffer_data, pick_pixel, get_texture_stats, export_draw_textures, save_render_target, export_mesh."""

from __future__ import annotations

import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from renderdoc_mcp.session import get_session
from renderdoc_mcp.util import (
    rd,
    to_json,
    make_error,
    enum_str,
    FILE_TYPE_MAP,
    SHADER_STAGE_MAP,
    MESH_DATA_STAGE_MAP,
    TOPOLOGY_MAP,
)


MAX_BUFFER_READ = 65536  # 64 KB limit


def register(mcp: FastMCP):
    @mcp.tool()
    def save_texture(
        resource_id: str,
        output_path: str,
        file_type: str = "png",
        mip: int = 0,
        event_id: Optional[int] = None,
    ) -> str:
        """Save a texture resource to an image file.

        Args:
            resource_id: The texture resource ID string.
            output_path: Absolute path for the output file.
            file_type: Output format: png, jpg, bmp, tga, hdr, exr, dds (default: png).
            mip: Mip level to save (default 0). Use -1 for all mips (DDS only).
            event_id: Optional event ID to navigate to first.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.ensure_event(event_id)
        if err:
            return to_json(err)

        tex_id = session.resolve_resource_id(resource_id)
        if tex_id is None:
            return to_json(make_error(f"Texture resource '{resource_id}' not found", "INVALID_RESOURCE_ID"))

        ft = FILE_TYPE_MAP.get(file_type.lower())
        if ft is None:
            return to_json(make_error(f"Unknown file type: {file_type}. Valid: {list(FILE_TYPE_MAP.keys())}", "API_ERROR"))

        output_path = os.path.normpath(output_path)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        texsave = rd.TextureSave()
        texsave.resourceId = tex_id
        texsave.destType = ft
        texsave.mip = mip
        texsave.slice.sliceIndex = 0
        texsave.alpha = rd.AlphaMapping.Preserve

        session.controller.SaveTexture(texsave, output_path)

        return to_json({
            "status": "saved",
            "output_path": output_path,
            "resource_id": resource_id,
            "file_type": file_type,
            "mip": mip,
        })

    @mcp.tool()
    def get_buffer_data(
        resource_id: str,
        offset: int = 0,
        length: int = 256,
        format: str = "hex",
    ) -> str:
        """Read raw data from a buffer resource.

        Args:
            resource_id: The buffer resource ID string.
            offset: Byte offset to start reading from (default 0).
            length: Number of bytes to read (default 256, max 65536).
            format: Output format: "hex" for hex dump, "floats" to interpret as float32 array.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        buf_id = session.resolve_resource_id(resource_id)
        if buf_id is None:
            return to_json(make_error(f"Buffer resource '{resource_id}' not found", "INVALID_RESOURCE_ID"))

        length = min(length, MAX_BUFFER_READ)
        data = session.controller.GetBufferData(buf_id, offset, length)

        result: dict = {
            "resource_id": resource_id,
            "offset": offset,
            "bytes_read": len(data),
        }

        if format == "floats":
            import struct
            num_floats = len(data) // 4
            floats = list(struct.unpack_from(f"{num_floats}f", data))
            result["data"] = [round(f, 6) for f in floats]
            result["format"] = "float32"
        else:
            # Hex dump - show lines of 16 bytes
            lines = []
            for i in range(0, len(data), 16):
                chunk = data[i:i+16]
                hex_part = " ".join(f"{b:02x}" for b in chunk)
                lines.append(f"{offset+i:08x}: {hex_part}")
            result["data"] = "\n".join(lines)
            result["format"] = "hex"

        return to_json(result)

    @mcp.tool()
    def pick_pixel(
        resource_id: str,
        x: int,
        y: int,
        event_id: Optional[int] = None,
    ) -> str:
        """Get the RGBA value of a specific pixel in a texture.

        Args:
            resource_id: The texture resource ID string.
            x: X coordinate of the pixel.
            y: Y coordinate of the pixel.
            event_id: Optional event ID to navigate to first.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.ensure_event(event_id)
        if err:
            return to_json(err)

        tex_id = session.resolve_resource_id(resource_id)
        if tex_id is None:
            return to_json(make_error(f"Texture resource '{resource_id}' not found", "INVALID_RESOURCE_ID"))

        # PickPixel returns a PixelValue
        val = session.controller.PickPixel(tex_id, x, y, rd.Subresource(0, 0, 0), rd.CompType.Typeless)

        return to_json({
            "resource_id": resource_id,
            "x": x,
            "y": y,
            "rgba": {
                "r": val.floatValue[0],
                "g": val.floatValue[1],
                "b": val.floatValue[2],
                "a": val.floatValue[3],
            },
            "rgba_uint": {
                "r": val.uintValue[0],
                "g": val.uintValue[1],
                "b": val.uintValue[2],
                "a": val.uintValue[3],
            },
        })

    @mcp.tool()
    def get_texture_stats(
        resource_id: str,
        event_id: Optional[int] = None,
        all_slices: bool = False,
    ) -> str:
        """Get min/max/average statistics for a texture at the current event.

        Args:
            resource_id: The texture resource ID string.
            event_id: Optional event ID to navigate to first.
            all_slices: If True, return per-slice/per-face statistics. Useful for
                        cubemaps (returns stats for each of the 6 faces) and texture arrays.
                        Default False returns only slice 0.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.ensure_event(event_id)
        if err:
            return to_json(err)

        tex_id = session.resolve_resource_id(resource_id)
        if tex_id is None:
            return to_json(make_error(f"Texture resource '{resource_id}' not found", "INVALID_RESOURCE_ID"))

        tex_desc = session.get_texture_desc(resource_id)

        def _minmax_to_dict(mm):
            mn, mx = mm[0], mm[1]
            r_min, g_min, b_min, a_min = mn.floatValue[0], mn.floatValue[1], mn.floatValue[2], mn.floatValue[3]
            r_max, g_max, b_max, a_max = mx.floatValue[0], mx.floatValue[1], mx.floatValue[2], mx.floatValue[3]
            has_neg = any(v < 0 for v in [r_min, g_min, b_min])
            import math
            has_inf = any(math.isinf(v) for v in [r_max, g_max, b_max])
            has_nan = any(math.isnan(v) for v in [r_min, g_min, b_min, r_max, g_max, b_max])
            d: dict = {
                "min": {"r": r_min, "g": g_min, "b": b_min, "a": a_min},
                "max": {"r": r_max, "g": g_max, "b": b_max, "a": a_max},
                "has_negative": has_neg,
                "has_inf": has_inf,
                "has_nan": has_nan,
            }
            warnings = []
            if has_nan:
                warnings.append("⚠️ NaN values detected")
            if has_inf:
                warnings.append("⚠️ Inf values detected")
            if has_neg:
                warnings.append("⚠️ Negative values detected")
            if warnings:
                d["warnings"] = warnings
            return d

        result: dict = {"resource_id": resource_id}
        if tex_desc is not None:
            result["name"] = tex_desc.name if hasattr(tex_desc, "name") else ""
            result["size"] = f"{tex_desc.width}x{tex_desc.height}"
            result["format"] = str(tex_desc.format.Name())
            result["mips"] = tex_desc.mips
            result["array_size"] = tex_desc.arraysize

        # Cubemap face names
        _FACE_NAMES = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]

        if all_slices and tex_desc is not None:
            num_slices = max(tex_desc.arraysize, 1)
            is_cubemap = num_slices == 6 or (hasattr(tex_desc, "dimension") and "Cube" in str(tex_desc.dimension))
            per_slice = []
            for s in range(num_slices):
                try:
                    mm = session.controller.GetMinMax(tex_id, rd.Subresource(0, s, 0), rd.CompType.Typeless)
                    entry = _minmax_to_dict(mm)
                    if is_cubemap and s < 6:
                        entry["face"] = _FACE_NAMES[s]
                    else:
                        entry["slice"] = s
                    per_slice.append(entry)
                except Exception as e:
                    per_slice.append({"slice": s, "error": str(e)})
            result["per_slice_stats"] = per_slice
        else:
            mm = session.controller.GetMinMax(tex_id, rd.Subresource(0, 0, 0), rd.CompType.Typeless)
            result.update(_minmax_to_dict(mm))

        return to_json(result)

    @mcp.tool()
    def read_texture_pixels(
        resource_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
        mip_level: int = 0,
        array_slice: int = 0,
        event_id: Optional[int] = None,
    ) -> str:
        """Read actual pixel values from a rectangular region of a texture.

        Returns float RGBA values for each pixel. Region is capped at 64x64.
        Useful for precisely checking IBL cubemap faces, LUT textures, history buffers, etc.

        Args:
            resource_id: The texture resource ID string.
            x: Top-left X coordinate of the region.
            y: Top-left Y coordinate of the region.
            width: Region width (max 64).
            height: Region height (max 64).
            mip_level: Mip level to read (default 0).
            array_slice: Array slice or cubemap face index (default 0).
                         Cubemap face order: 0=+X 1=-X 2=+Y 3=-Y 4=+Z 5=-Z.
            event_id: Optional event ID to navigate to first.
        """
        import struct as _struct
        import math as _math

        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.ensure_event(event_id)
        if err:
            return to_json(err)

        tex_id = session.resolve_resource_id(resource_id)
        if tex_id is None:
            return to_json(make_error(f"Texture resource '{resource_id}' not found", "INVALID_RESOURCE_ID"))

        width = min(width, 64)
        height = min(height, 64)

        pixels: list[list] = []
        anomalies: list[dict] = []

        for py in range(y, y + height):
            row: list = []
            for px in range(x, x + width):
                try:
                    val = session.controller.PickPixel(
                        tex_id, px, py,
                        rd.Subresource(mip_level, array_slice, 0),
                        rd.CompType.Typeless,
                    )
                    r, g, b, a = (val.floatValue[0], val.floatValue[1],
                                  val.floatValue[2], val.floatValue[3])
                    pixel = [round(r, 6), round(g, 6), round(b, 6), round(a, 6)]
                    row.append(pixel)

                    # Anomaly detection
                    for ch_idx, ch_val in enumerate(pixel[:3]):
                        ch_name = "rgba"[ch_idx]
                        if _math.isnan(ch_val):
                            anomalies.append({"x": px, "y": py, "channel": ch_name, "type": "NaN"})
                        elif _math.isinf(ch_val):
                            anomalies.append({"x": px, "y": py, "channel": ch_name, "type": "Inf", "value": str(ch_val)})
                        elif ch_val < 0:
                            anomalies.append({"x": px, "y": py, "channel": ch_name, "type": "negative", "value": round(ch_val, 6)})
                except Exception as e:
                    row.append({"error": str(e)})
            pixels.append(row)

        result: dict = {
            "resource_id": resource_id,
            "region": {"x": x, "y": y, "width": width, "height": height},
            "mip_level": mip_level,
            "array_slice": array_slice,
            "pixels": pixels,
        }
        if anomalies:
            result["anomalies"] = anomalies
            result["anomaly_count"] = len(anomalies)
        return to_json(result)

    @mcp.tool()
    def export_draw_textures(
        event_id: int,
        output_dir: str,
        skip_small: bool = True,
    ) -> str:
        """Batch export all textures bound to a draw call's pixel shader.

        Args:
            event_id: The event ID of the draw call.
            output_dir: Directory to save exported textures.
            skip_small: Skip textures 4x4 or smaller (placeholder textures). Default True.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.set_event(event_id)
        if err:
            return to_json(err)

        state = session.controller.GetPipelineState()
        ps_refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
        if ps_refl is None:
            return to_json(make_error("No pixel shader bound at this event", "API_ERROR"))

        output_dir = os.path.normpath(output_dir)
        os.makedirs(output_dir, exist_ok=True)

        exported = []
        skipped = []
        try:
            all_ro = state.GetReadOnlyResources(rd.ShaderStage.Pixel)
            ro_by_index: dict = {}
            for b in all_ro:
                ro_by_index.setdefault(b.access.index, []).append(b)
        except Exception:
            ro_by_index = {}

        for i, ro_refl in enumerate(ps_refl.readOnlyResources):
            for b in ro_by_index.get(i, []):
                rid_str = str(b.descriptor.resource)
                tex_desc = session.get_texture_desc(rid_str)
                if tex_desc is None:
                    continue

                if skip_small and tex_desc.width <= 4 and tex_desc.height <= 4:
                    skipped.append({"name": ro_refl.name, "resource_id": rid_str,
                                    "size": f"{tex_desc.width}x{tex_desc.height}"})
                    continue

                filename = f"{ro_refl.name}_{tex_desc.width}x{tex_desc.height}.png"
                # Sanitize filename
                filename = filename.replace("/", "_").replace("\\", "_")
                out_path = os.path.join(output_dir, filename)

                texsave = rd.TextureSave()
                texsave.resourceId = tex_desc.resourceId
                texsave.destType = rd.FileType.PNG
                texsave.mip = 0
                texsave.slice.sliceIndex = 0
                texsave.alpha = rd.AlphaMapping.Preserve
                session.controller.SaveTexture(texsave, out_path)

                exported.append({
                    "name": ro_refl.name,
                    "resource_id": rid_str,
                    "size": f"{tex_desc.width}x{tex_desc.height}",
                    "output_path": out_path,
                })

        return to_json({
            "event_id": event_id,
            "exported": exported,
            "exported_count": len(exported),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @mcp.tool()
    def save_render_target(
        event_id: int,
        output_path: str,
        save_depth: bool = False,
    ) -> str:
        """Save the current render target(s) at a specific event.

        Args:
            event_id: The event ID to capture the render target from.
            output_path: Output file path or directory. If directory, auto-names the file.
            save_depth: Also save the depth target (default False).
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.set_event(event_id)
        if err:
            return to_json(err)

        state = session.controller.GetPipelineState()
        output_path = os.path.normpath(output_path)
        saved = []

        # Save color target
        outputs = state.GetOutputTargets()
        color_target = None
        for o in outputs:
            if int(o.resource) != 0:
                color_target = o
                break

        if color_target is None:
            return to_json(make_error("No color render target bound at this event", "API_ERROR"))

        rid_str = str(color_target.resource)
        tex_desc = session.get_texture_desc(rid_str)

        if os.path.isdir(output_path):
            fname = f"rt_color_eid{event_id}.png"
            color_path = os.path.join(output_path, fname)
        else:
            color_path = output_path
            os.makedirs(os.path.dirname(color_path) or ".", exist_ok=True)

        texsave = rd.TextureSave()
        texsave.resourceId = color_target.resource
        texsave.destType = rd.FileType.PNG
        texsave.mip = 0
        texsave.slice.sliceIndex = 0
        texsave.alpha = rd.AlphaMapping.Preserve
        session.controller.SaveTexture(texsave, color_path)

        color_info: dict = {"type": "color", "resource_id": rid_str, "output_path": color_path}
        if tex_desc is not None:
            color_info["size"] = f"{tex_desc.width}x{tex_desc.height}"
            color_info["format"] = str(tex_desc.format.Name())
        saved.append(color_info)

        # Optionally save depth target
        if save_depth:
            try:
                dt = state.GetDepthTarget()
                if int(dt.resource) != 0:
                    dt_rid = str(dt.resource)
                    if os.path.isdir(output_path):
                        depth_path = os.path.join(output_path, f"rt_depth_eid{event_id}.png")
                    else:
                        base, ext = os.path.splitext(color_path)
                        depth_path = f"{base}_depth{ext}"

                    texsave = rd.TextureSave()
                    texsave.resourceId = dt.resource
                    texsave.destType = rd.FileType.PNG
                    texsave.mip = 0
                    texsave.slice.sliceIndex = 0
                    texsave.alpha = rd.AlphaMapping.Preserve
                    session.controller.SaveTexture(texsave, depth_path)

                    depth_info: dict = {"type": "depth", "resource_id": dt_rid, "output_path": depth_path}
                    dt_desc = session.get_texture_desc(dt_rid)
                    if dt_desc is not None:
                        depth_info["size"] = f"{dt_desc.width}x{dt_desc.height}"
                        depth_info["format"] = str(dt_desc.format.Name())
                    saved.append(depth_info)
            except Exception as exc:
                saved.append({"type": "depth", "error": f"Failed to save depth: {exc}"})

        return to_json({"event_id": event_id, "saved": saved, "count": len(saved)})

    @mcp.tool()
    def export_mesh(
        event_id: int,
        output_path: str,
    ) -> str:
        """Export mesh data from a draw call as OBJ format.

        Uses post-vertex-shader data to get transformed positions, normals, and UVs.

        Args:
            event_id: The event ID of the draw call.
            output_path: Output file path for the .obj file.
        """
        import struct as _struct

        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.set_event(event_id)
        if err:
            return to_json(err)

        postvs = session.controller.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
        if postvs.vertexResourceId == rd.ResourceId.Null():
            return to_json(make_error("No post-VS data available for this event", "API_ERROR"))

        state = session.controller.GetPipelineState()
        vs_refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
        if vs_refl is None:
            return to_json(make_error("No vertex shader bound", "API_ERROR"))

        # Parse output signature to find position/normal/uv offsets
        out_sig = vs_refl.outputSignature
        pos_idx = None
        norm_idx = None
        uv_idx = None
        offset_map: list[tuple[str, int, int]] = []  # (semantic, start_float, comp_count)
        float_offset = 0
        for sig in out_sig:
            name = (sig.semanticName or sig.varName or "").upper()
            if "POSITION" in name:
                pos_idx = float_offset
            elif "NORMAL" in name:
                norm_idx = float_offset
            elif "TEXCOORD" in name and uv_idx is None:
                uv_idx = float_offset
            offset_map.append((name, float_offset, sig.compCount))
            float_offset += sig.compCount

        if pos_idx is None:
            # Fallback: assume first 4 floats are position
            pos_idx = 0

        num_verts = postvs.numIndices
        data = session.controller.GetBufferData(
            postvs.vertexResourceId, postvs.vertexByteOffset,
            num_verts * postvs.vertexByteStride,
        )

        floats_per_vertex = postvs.vertexByteStride // 4
        if floats_per_vertex == 0:
            return to_json(make_error(
                f"Invalid vertex stride ({postvs.vertexByteStride} bytes), cannot parse mesh data",
                "API_ERROR",
            ))

        # Parse all vertices
        positions = []
        normals = []
        uvs = []
        for i in range(num_verts):
            off = i * postvs.vertexByteStride
            if off + postvs.vertexByteStride > len(data):
                break
            vfloats = list(_struct.unpack_from(f"{floats_per_vertex}f", data, off))

            # Position (x, y, z) - skip w
            if pos_idx is not None and pos_idx + 3 <= len(vfloats):
                positions.append((vfloats[pos_idx], vfloats[pos_idx + 1], vfloats[pos_idx + 2]))
            else:
                positions.append((0.0, 0.0, 0.0))

            if norm_idx is not None and norm_idx + 3 <= len(vfloats):
                normals.append((vfloats[norm_idx], vfloats[norm_idx + 1], vfloats[norm_idx + 2]))

            if uv_idx is not None and uv_idx + 2 <= len(vfloats):
                uvs.append((vfloats[uv_idx], vfloats[uv_idx + 1]))

        # Write OBJ
        output_path = os.path.normpath(output_path)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        # Determine topology for correct face generation
        try:
            topo = state.GetPrimitiveTopology()
            topo_name = enum_str(topo, TOPOLOGY_MAP, "Topology.")
        except Exception:
            topo_name = "TriangleList"

        lines = [f"# Exported from RenderDoc MCP - event {event_id}"]
        lines.append(f"# Vertices: {len(positions)}, Topology: {topo_name}")

        for p in positions:
            lines.append(f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}")

        for n in normals:
            lines.append(f"vn {n[0]:.6f} {n[1]:.6f} {n[2]:.6f}")

        for uv in uvs:
            lines.append(f"vt {uv[0]:.6f} {uv[1]:.6f}")

        # Build triangle indices based on topology
        has_normals = len(normals) == len(positions)
        has_uvs = len(uvs) == len(positions)
        triangles: list[tuple[int, int, int]] = []  # 0-indexed

        if topo_name == "TriangleStrip":
            for i in range(len(positions) - 2):
                if i % 2 == 0:
                    triangles.append((i, i + 1, i + 2))
                else:
                    triangles.append((i, i + 2, i + 1))  # flip winding
        elif topo_name == "TriangleFan":
            for i in range(1, len(positions) - 1):
                triangles.append((0, i, i + 1))
        else:
            # TriangleList and all other topologies default to list
            for i in range(0, len(positions) - 2, 3):
                triangles.append((i, i + 1, i + 2))

        for t in triangles:
            i1, i2, i3 = t[0] + 1, t[1] + 1, t[2] + 1  # OBJ is 1-indexed
            if has_normals and has_uvs:
                lines.append(f"f {i1}/{i1}/{i1} {i2}/{i2}/{i2} {i3}/{i3}/{i3}")
            elif has_normals:
                lines.append(f"f {i1}//{i1} {i2}//{i2} {i3}//{i3}")
            elif has_uvs:
                lines.append(f"f {i1}/{i1} {i2}/{i2} {i3}/{i3}")
            else:
                lines.append(f"f {i1} {i2} {i3}")

        with open(output_path, "w") as f:
            f.write("\n".join(lines) + "\n")

        return to_json({
            "event_id": event_id,
            "output_path": output_path,
            "topology": topo_name,
            "vertices": len(positions),
            "normals": len(normals),
            "uvs": len(uvs),
            "triangles": len(triangles),
        })
