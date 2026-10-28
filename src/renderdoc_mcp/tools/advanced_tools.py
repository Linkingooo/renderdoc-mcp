"""Advanced tools: pixel_history, get_post_vs_data, diff_draw_calls, analyze_render_passes,
sample_pixel_region, debug_shader_at_pixel."""

from __future__ import annotations

import math
import struct
from typing import Optional

from mcp.server.fastmcp import FastMCP

from renderdoc_mcp.session import get_session
from renderdoc_mcp.util import (
    rd,
    to_json,
    make_error,
    flags_to_list,
    MESH_DATA_STAGE_MAP,
    serialize_shader_variable,
    SHADER_STAGE_MAP,
)


def register(mcp: FastMCP):

    @mcp.tool()
    def sample_pixel_region(
        event_id: Optional[int] = None,
        resource_id: Optional[str] = None,
        region: Optional[dict] = None,
        sample_count: int = 256,
        anomaly_threshold: float = 10.0,
    ) -> str:
        """Batch-sample pixels from a render target and auto-detect anomalies.

        Scans for NaN / Inf / negative values and extreme-bright pixels.
        Best tool for locating anomalous color values and IBL / HDR issues.

        Args:
            event_id: Sample at this event's render target state. Uses current event if omitted.
            resource_id: Specific render target resource ID. If omitted, uses the first
                         color output of the current event.
            region: Optional sampling region {"x":0,"y":0,"width":W,"height":H}.
                    If omitted, samples the full texture uniformly.
            sample_count: Number of sample points (default 256, max 1024).
            anomaly_threshold: Pixels with any channel exceeding this value are flagged
                               as extreme-bright (useful for HDR targets). Default 10.0.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.ensure_event(event_id)
        if err:
            return to_json(err)

        # Resolve render target
        if resource_id is not None:
            tex_id = session.resolve_resource_id(resource_id)
            if tex_id is None:
                return to_json(make_error(f"Resource '{resource_id}' not found", "INVALID_RESOURCE_ID"))
            tex_desc = session.get_texture_desc(resource_id)
        else:
            state = session.controller.GetPipelineState()
            try:
                outputs = state.GetOutputTargets()
                color_target = next((o for o in outputs if int(o.resource) != 0), None)
            except Exception:
                color_target = None
            if color_target is None:
                return to_json(make_error("No color render target at current event", "API_ERROR"))
            rid_str = str(color_target.resource)
            tex_id = session.resolve_resource_id(rid_str)
            tex_desc = session.get_texture_desc(rid_str)
            resource_id = rid_str

        if tex_desc is None:
            return to_json(make_error("Could not get texture description", "API_ERROR"))

        # Determine sampling region
        tex_w, tex_h = tex_desc.width, tex_desc.height
        if region:
            rx = max(0, region.get("x", 0))
            ry = max(0, region.get("y", 0))
            rw = min(region.get("width", tex_w), tex_w - rx)
            rh = min(region.get("height", tex_h), tex_h - ry)
        else:
            rx, ry, rw, rh = 0, 0, tex_w, tex_h

        sample_count = min(sample_count, 1024)

        # Generate a uniform grid of sample points
        import math as _math
        cols = max(1, int(_math.sqrt(sample_count * rw / max(rh, 1))))
        rows = max(1, sample_count // cols)
        step_x = max(1, rw // cols)
        step_y = max(1, rh // rows)

        sample_points: list[tuple[int, int]] = []
        for r in range(rows):
            for c in range(cols):
                px = rx + c * step_x + step_x // 2
                py = ry + r * step_y + step_y // 2
                if px < rx + rw and py < ry + rh:
                    sample_points.append((px, py))

        nan_count = inf_count = neg_count = bright_count = 0
        hotspots: list[dict] = []
        values_r: list[float] = []
        values_g: list[float] = []
        values_b: list[float] = []

        for px, py in sample_points:
            try:
                val = session.controller.PickPixel(
                    tex_id, px, py, rd.Subresource(0, 0, 0), rd.CompType.Typeless
                )
                r, g, b = val.floatValue[0], val.floatValue[1], val.floatValue[2]
            except Exception:
                continue

            pixel = [round(r, 6), round(g, 6), round(b, 6)]
            anomaly_type: str | None = None

            if any(_math.isnan(v) for v in [r, g, b]):
                nan_count += 1
                anomaly_type = "NaN"
            elif any(_math.isinf(v) for v in [r, g, b]):
                inf_count += 1
                anomaly_type = "Inf"
                pixel = ["+Inf" if _math.isinf(v) and v > 0 else ("-Inf" if _math.isinf(v) else v) for v in pixel]
            elif any(v < 0 for v in [r, g, b]):
                neg_count += 1
                anomaly_type = "negative"
            elif max(r, g, b) > anomaly_threshold:
                bright_count += 1
                anomaly_type = "extreme_bright"

            if anomaly_type:
                hotspots.append({"pixel": [px, py], "value": pixel, "type": anomaly_type})

            if not any(_math.isnan(v) or _math.isinf(v) for v in [r, g, b]):
                values_r.append(r)
                values_g.append(g)
                values_b.append(b)

        total = len(sample_points)
        anomaly_total = nan_count + inf_count + neg_count + bright_count

        stats: dict = {}
        for ch, vals in [("r", values_r), ("g", values_g), ("b", values_b)]:
            if vals:
                stats[ch] = {
                    "min": round(min(vals), 6),
                    "max": round(max(vals), 6),
                    "mean": round(sum(vals) / len(vals), 6),
                }

        # Limit hotspots returned
        MAX_HOTSPOTS = 20
        hotspots_out = hotspots[:MAX_HOTSPOTS]

        # Build diagnosis hint
        hints: list[str] = []
        if inf_count > 0:
            hints.append(f"{inf_count} Inf pixels - run pixel_history on them to trace the source")
        if neg_count > 0:
            hints.append(f"{neg_count} negative pixels - possibly from IBL SH sampling or HDR overflow; check the draw calls writing this RT")
        if nan_count > 0:
            hints.append(f"{nan_count} NaN pixels - usually caused by division by zero or 0/0")
        if bright_count > 0:
            hints.append(f"{bright_count} very bright pixels (>{anomaly_threshold}) - possible overflow or exposure anomaly")

        result = {
            "resource_id": resource_id,
            "render_target": f"{getattr(tex_desc, 'name', None) or resource_id} ({tex_desc.format.Name()}) {tex_w}x{tex_h}",
            "total_samples": total,
            "anomalies": {
                "nan_count": nan_count,
                "inf_count": inf_count,
                "negative_count": neg_count,
                "extreme_bright_count": bright_count,
                "anomaly_total": anomaly_total,
                "anomaly_rate": f"{anomaly_total / max(total, 1) * 100:.1f}%",
            },
            "hotspots": hotspots_out,
            "statistics": stats,
        }
        if hints:
            result["diagnosis_hint"] = " | ".join(hints)
        return to_json(result)

    @mcp.tool()
    def debug_shader_at_pixel(
        event_id: int,
        pixel_x: int,
        pixel_y: int,
        stage: str = "pixel",
        watch_variables: Optional[list[str]] = None,
    ) -> str:
        """Debug the shader at a specific pixel using RenderDoc's shader debugger.

        Executes the shader for the given pixel and returns intermediate variable
        values at each step. Useful for tracing negative values, IBL computation
        errors, TAA artifacts, and other precision issues.

        Note: Shader debugging may not be supported for all API/GPU combinations.
        If unsupported, falls back to reporting pixel value and bound shader info.

        Args:
            event_id: The event ID of the draw call.
            pixel_x: X coordinate (render target space, origin top-left).
            pixel_y: Y coordinate.
            stage: Shader stage to debug: "pixel" (default) or "vertex".
            watch_variables: Optional list of variable names to focus on
                             (e.g. ["color", "iblDiffuse", "exposure"]).
                             If omitted, all variables are returned (may be large).
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.set_event(event_id)
        if err:
            return to_json(err)

        stage_enum = SHADER_STAGE_MAP.get(stage.lower())
        if stage_enum is None:
            return to_json(make_error(f"Unknown shader stage: {stage}", "API_ERROR"))

        state = session.controller.GetPipelineState()
        refl = state.GetShaderReflection(stage_enum)
        if refl is None:
            return to_json(make_error(f"No shader bound at stage '{stage}'", "API_ERROR"))

        # Collect pixel value before debugging
        pixel_val_info: dict = {}
        try:
            outputs = state.GetOutputTargets()
            if outputs:
                first_rt = next((o for o in outputs if int(o.resource) != 0), None)
                if first_rt:
                    rt_id = first_rt.resource
                    pv = session.controller.PickPixel(
                        rt_id, pixel_x, pixel_y,
                        rd.Subresource(0, 0, 0), rd.CompType.Typeless
                    )
                    pixel_val_info = {
                        "current_pixel_rgba": [
                            round(pv.floatValue[0], 6), round(pv.floatValue[1], 6),
                            round(pv.floatValue[2], 6), round(pv.floatValue[3], 6),
                        ],
                    }
                    r, g, b = pv.floatValue[0], pv.floatValue[1], pv.floatValue[2]
                    anomalies = []
                    if any(math.isnan(v) for v in [r, g, b]):
                        anomalies.append("NaN detected in output pixel")
                    if any(math.isinf(v) for v in [r, g, b]):
                        anomalies.append("Inf detected in output pixel")
                    if any(v < 0 for v in [r, g, b]):
                        anomalies.append(f"Negative value in output: [{r:.4f}, {g:.4f}, {b:.4f}]")
                    if anomalies:
                        pixel_val_info["pixel_anomalies"] = anomalies
        except Exception:
            pass

        # Attempt shader debugging
        trace_result: dict | None = None
        debug_error: str | None = None
        try:
            if stage_enum == rd.ShaderStage.Pixel:
                shader_debug = session.controller.DebugPixel(
                    pixel_x, pixel_y,
                    rd.DebugPixelInputs(),
                )
            else:
                shader_debug = None

            if shader_debug is not None and hasattr(shader_debug, "states") and shader_debug.states:
                states = shader_debug.states
                steps: list[dict] = []

                for step_state in states:
                    step: dict = {}
                    # Try to get changed variables at this step
                    try:
                        changed = []
                        if hasattr(step_state, "locals"):
                            for var in step_state.locals:
                                vname = var.name
                                if watch_variables and not any(w.lower() in vname.lower() for w in watch_variables):
                                    continue
                                sv = serialize_shader_variable(var)
                                # Annotate anomalies
                                val = sv.get("value", [])
                                if isinstance(val, list):
                                    flat = [v for v in val if isinstance(v, (int, float))]
                                    if any(math.isnan(v) for v in flat):
                                        sv["warning"] = f"⚠️ {vname} contains NaN"
                                    elif any(math.isinf(v) for v in flat):
                                        sv["warning"] = f"⚠️ {vname} contains Inf"
                                    elif any(v < 0 for v in flat):
                                        sv["warning"] = f"⚠️ {vname} contains negative values: {flat}"
                                changed.append(sv)
                        if changed:
                            step["variables"] = changed
                    except Exception:
                        pass

                    if step:
                        steps.append(step)

                trace_result = {
                    "steps_with_changes": len(steps),
                    "trace": steps[:200],  # cap output
                }
            else:
                debug_error = "DebugPixel returned no trace data (may not be supported for this API/GPU)"
        except Exception as e:
            debug_error = f"Shader debugging failed: {type(e).__name__}: {e}"

        result: dict = {
            "event_id": event_id,
            "pixel": [pixel_x, pixel_y],
            "stage": stage,
            "shader_resource_id": str(refl.resourceId),
            "entry_point": refl.entryPoint,
        }
        result.update(pixel_val_info)

        if trace_result is not None:
            result["debug_trace"] = trace_result
        else:
            result["debug_note"] = debug_error or "Shader trace unavailable"
            result["fallback_info"] = {
                "constant_blocks": [cb.name for cb in refl.constantBlocks],
                "read_only_resources": [r.name for r in refl.readOnlyResources],
                "suggestion": "Use get_cbuffer_contents and pixel_history for manual investigation",
            }

        return to_json(result)

    @mcp.tool()
    def pixel_history(
        resource_id: str,
        x: int,
        y: int,
        event_id: Optional[int] = None,
    ) -> str:
        """Get the full modification history of a pixel across all events in the frame.

        Shows every event that wrote to this pixel, with before/after values and
        pass/fail status (depth test, stencil test, etc.).

        Args:
            resource_id: The texture resource ID (must be a render target).
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

        history = session.controller.PixelHistory(
            tex_id, x, y, rd.Subresource(0, 0, 0), rd.CompType.Typeless,
        )

        results = []
        for mod in history:
            passed = mod.Passed()
            entry: dict = {
                "event_id": mod.eventId,
                "passed": passed,
            }
            # Add failure reasons when the modification did not pass
            if not passed:
                failure_reasons = []
                try:
                    if mod.backfaceCulled:
                        failure_reasons.append("backface_culled")
                    if mod.depthTestFailed:
                        failure_reasons.append("depth_test_failed")
                    if mod.stencilTestFailed:
                        failure_reasons.append("stencil_test_failed")
                    if mod.scissorClipped:
                        failure_reasons.append("scissor_clipped")
                    if mod.shaderDiscarded:
                        failure_reasons.append("shader_discarded")
                    if mod.depthClipped:
                        failure_reasons.append("depth_clipped")
                except Exception:
                    pass
                if failure_reasons:
                    entry["failure_reasons"] = failure_reasons
            # Pre-modification value
            pre = mod.preMod
            entry["pre_value"] = {
                "r": pre.col.floatValue[0],
                "g": pre.col.floatValue[1],
                "b": pre.col.floatValue[2],
                "a": pre.col.floatValue[3],
                "depth": pre.depth,
                "stencil": pre.stencil,
            }
            # Post-modification value
            post = mod.postMod
            entry["post_value"] = {
                "r": post.col.floatValue[0],
                "g": post.col.floatValue[1],
                "b": post.col.floatValue[2],
                "a": post.col.floatValue[3],
                "depth": post.depth,
                "stencil": post.stencil,
            }

            # Check if pixel actually changed
            entry["pixel_changed"] = (
                pre.col.floatValue[0] != post.col.floatValue[0]
                or pre.col.floatValue[1] != post.col.floatValue[1]
                or pre.col.floatValue[2] != post.col.floatValue[2]
                or pre.col.floatValue[3] != post.col.floatValue[3]
            )

            results.append(entry)

        return to_json({
            "resource_id": resource_id,
            "x": x,
            "y": y,
            "modifications": results,
            "count": len(results),
        })

    @mcp.tool()
    def get_post_vs_data(
        stage: str = "vsout",
        max_vertices: int = 100,
        event_id: Optional[int] = None,
    ) -> str:
        """Get post-vertex-shader transformed vertex data for the current draw call.

        Args:
            stage: Data stage: "vsin" (vertex input), "vsout" (after vertex shader),
                  "gsout" (after geometry shader). Default: "vsout".
            max_vertices: Maximum number of vertices to return (default 100).
            event_id: Optional event ID to navigate to first.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.ensure_event(event_id)
        if err:
            return to_json(err)

        mesh_stage = MESH_DATA_STAGE_MAP.get(stage.lower())
        if mesh_stage is None:
            return to_json(make_error(
                f"Unknown mesh stage: {stage}. Valid: {list(MESH_DATA_STAGE_MAP.keys())}",
                "API_ERROR",
            ))

        postvs = session.controller.GetPostVSData(0, 0, mesh_stage)

        if postvs.vertexResourceId == rd.ResourceId.Null():
            return to_json(make_error("No post-VS data available for current event", "API_ERROR"))

        # Get the output signature to know the attribute layout
        state = session.controller.GetPipelineState()
        if stage.lower() == "vsin":
            # For vertex input, use vertex input attributes
            attrs = state.GetVertexInputs()
            attr_info = [{"name": a.name, "format": str(a.format.Name())} for a in attrs]
        else:
            # Use the appropriate shader stage reflection for output signature
            refl_stage = rd.ShaderStage.Vertex
            if stage.lower() == "gsout":
                gs_refl = state.GetShaderReflection(rd.ShaderStage.Geometry)
                if gs_refl is not None:
                    refl_stage = rd.ShaderStage.Geometry
            vs_refl = state.GetShaderReflection(refl_stage)
            if vs_refl is None:
                return to_json(make_error("No shader bound for requested stage", "API_ERROR"))
            attr_info = []
            for sig in vs_refl.outputSignature:
                name = sig.semanticIdxName if sig.varName == "" else sig.varName
                attr_info.append({
                    "name": name,
                    "var_type": str(sig.varType),
                    "comp_count": sig.compCount,
                    "system_value": str(sig.systemValue),
                })

        # Read vertex data
        num_verts = min(postvs.numIndices, max_vertices)
        data = session.controller.GetBufferData(
            postvs.vertexResourceId, postvs.vertexByteOffset,
            num_verts * postvs.vertexByteStride,
        )

        # Parse vertices as float arrays
        vertices = []
        floats_per_vertex = postvs.vertexByteStride // 4
        if floats_per_vertex == 0:
            return to_json(make_error(
                f"Invalid vertex stride ({postvs.vertexByteStride} bytes), cannot parse vertex data",
                "API_ERROR",
            ))
        for i in range(num_verts):
            offset = i * postvs.vertexByteStride
            if offset + postvs.vertexByteStride > len(data):
                break
            vertex_floats = list(struct.unpack_from(
                f"{floats_per_vertex}f", data, offset
            ))
            vertices.append([round(f, 6) for f in vertex_floats])

        return to_json({
            "stage": stage,
            "event_id": session.current_event,
            "attributes": attr_info,
            "vertex_stride": postvs.vertexByteStride,
            "total_vertices": postvs.numIndices,
            "returned_vertices": len(vertices),
            "vertices": vertices,
        })

    @mcp.tool()
    def diff_draw_calls(eid1: int, eid2: int) -> str:
        """Compare two draw calls and return their state differences.

        Useful for understanding what changed between two similar draw calls.

        Args:
            eid1: Event ID of the first draw call.
            eid2: Event ID of the second draw call.
        """
        from renderdoc_mcp.tools.pipeline_tools import _get_draw_state_dict

        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        state1 = _get_draw_state_dict(session, eid1)
        if "error" in state1:
            return to_json(state1)

        state2 = _get_draw_state_dict(session, eid2)
        if "error" in state2:
            return to_json(state2)

        raw_diff = _diff_dicts(state1, state2)
        differences = _add_implications(raw_diff)

        return to_json({
            "eid1": eid1,
            "eid2": eid2,
            "differences": differences,
            "identical": len(differences) == 0,
            "summary": (
                f"Found {len(differences)} difference(s)" if differences
                else "The two draw calls have identical pipeline state"
            ),
        })

    @mcp.tool()
    def analyze_render_passes() -> str:
        """Auto-detect render pass boundaries and summarize each pass.

        Detects passes by Clear actions and output target changes.
        Returns a list of render passes with draw count, RT info, and event range.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        sf = session.structured_file
        passes: list[dict] = []
        current_pass: dict | None = None
        last_outputs: tuple | None = None

        for eid in sorted(session.action_map.keys()):
            action = session.action_map[eid]
            is_clear = bool(action.flags & rd.ActionFlags.Clear)
            is_draw = bool(action.flags & rd.ActionFlags.Drawcall)

            if not is_clear and not is_draw:
                continue

            # Determine current outputs
            outputs = tuple(str(o) for o in action.outputs if int(o) != 0)

            # Detect pass boundary: clear or output target change
            new_pass = False
            if is_clear:
                new_pass = True
            elif outputs and outputs != last_outputs:
                new_pass = True

            if new_pass:
                # Start a new pass
                if current_pass is not None:
                    passes.append(current_pass)
                current_pass = {
                    "pass_index": len(passes),
                    "start_event": eid,
                    "end_event": eid,
                    "start_action": action.GetName(sf),
                    "draw_count": 0,
                    "clear_count": 0,
                    "render_targets": list(outputs) if outputs else [],
                }

            if current_pass is None:
                current_pass = {
                    "pass_index": 0,
                    "start_event": eid,
                    "end_event": eid,
                    "start_action": action.GetName(sf),
                    "draw_count": 0,
                    "clear_count": 0,
                    "render_targets": list(outputs) if outputs else [],
                }

            current_pass["end_event"] = eid
            if is_draw:
                current_pass["draw_count"] += 1
            if is_clear:
                current_pass["clear_count"] += 1

            if outputs:
                last_outputs = outputs
                # Update RT info if changed within pass
                for o in outputs:
                    if o not in current_pass["render_targets"]:
                        current_pass["render_targets"].append(o)

        if current_pass is not None:
            passes.append(current_pass)

        # Enrich with RT size info
        for p in passes:
            rt_info = []
            for rid_str in p["render_targets"]:
                entry: dict = {"resource_id": rid_str}
                tex_desc = session.get_texture_desc(rid_str)
                if tex_desc is not None:
                    entry["size"] = f"{tex_desc.width}x{tex_desc.height}"
                    entry["format"] = str(tex_desc.format.Name())
                rt_info.append(entry)
            p["render_target_info"] = rt_info

        return to_json({
            "passes": passes,
            "total_passes": len(passes),
        })


def _diff_dicts(d1: dict, d2: dict, path: str = "") -> dict:
    """Recursively diff two dicts, returning only differing keys."""
    diff: dict = {}
    all_keys = set(d1.keys()) | set(d2.keys())

    for key in all_keys:
        key_path = f"{path}.{key}" if path else key
        v1 = d1.get(key)
        v2 = d2.get(key)

        if v1 == v2:
            continue

        if isinstance(v1, dict) and isinstance(v2, dict):
            sub = _diff_dicts(v1, v2, key_path)
            if sub:
                diff[key] = sub
        elif isinstance(v1, list) and isinstance(v2, list):
            if v1 != v2:
                diff[key] = {"eid1": v1, "eid2": v2}
        else:
            diff[key] = {"eid1": v1, "eid2": v2}

    return diff


# ── Implication rules for diff_draw_calls ──
# Each entry: (field_path_suffix, implication_template)
def _implication_for(suffix: str, v1, v2) -> str | None:
    """Return a human-readable implication string for a known diff field."""
    s1, s2 = str(v1), str(v2)
    if suffix == "blend.enabled":
        detail = "Blending enabled; color is blended with the background" if v2 else "Blending disabled; output overwrites the target pixel (opaque mode)"
        return f"Blend toggle: {s1}→{s2}. {detail}"
    if suffix == "blend.color_src":
        return f"Color source blend factor: {s1}→{s2}. May change output color brightness/opacity"
    if suffix == "blend.color_dst":
        return f"Color destination blend factor: {s1}→{s2}. Changes the weight of the background color in the result"
    if suffix == "blend.color_op":
        return f"Color blend operation: {s1}→{s2}. A different operation may shift the overall color"
    if suffix == "blend.alpha_src":
        return f"Alpha source blend factor: {s1}→{s2}"
    if suffix == "depth.test":
        detail = "Objects may pass through other geometry when disabled" if not v2 else "Make sure the depth buffer is initialized correctly when enabled"
        return f"Depth test: {s1}→{s2}. {detail}"
    if suffix == "depth.write":
        detail = "When disabled, this draw call does not update the depth buffer (suits transparent objects)" if not v2 else "When enabled, depth values are updated"
        return f"Depth write: {s1}→{s2}. {detail}"
    if suffix == "depth.func":
        return f"Depth compare function: {s1}→{s2}. May cause occlusion order changes or objects disappearing"
    if suffix == "stencil.enabled":
        return f"Stencil test: {s1}→{s2}"
    if suffix == "rasterizer.cull":
        return f"Cull mode: {s1}→{s2}. May affect back/front face visibility; reflection passes usually need inverted culling"
    if suffix == "rasterizer.front_ccw":
        return f"Front face: {s1}→{s2}. Switching CCW/CW flips the back-face culling direction"
    if suffix == "topology":
        return f"Primitive topology: {s1}→{s2}"
    return None

_IMPLICATION_SUFFIXES = [
    "blend.enabled", "blend.color_src", "blend.color_dst", "blend.color_op",
    "blend.alpha_src", "depth.test", "depth.write", "depth.func",
    "stencil.enabled", "rasterizer.cull", "rasterizer.front_ccw", "topology",
]


def _add_implications(diff: dict) -> list[dict]:
    """Flatten a nested diff dict into a list with implication annotations."""
    results: list[dict] = []

    def _flatten(d: dict, path: str):
        for key, val in d.items():
            key_path = f"{path}.{key}" if path else key
            if isinstance(val, dict):
                if "eid1" in val or "eid2" in val:
                    v1 = val.get("eid1")
                    v2 = val.get("eid2")
                    entry: dict = {"field": key_path, "eid1": v1, "eid2": v2}
                    for suffix in _IMPLICATION_SUFFIXES:
                        if key_path.endswith(suffix):
                            imp = _implication_for(suffix, v1, v2)
                            if imp:
                                entry["implication"] = imp
                            break
                    results.append(entry)
                else:
                    _flatten(val, key_path)

    _flatten(diff, "")
    return results
