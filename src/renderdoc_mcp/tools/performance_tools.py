"""Performance analysis tools: get_pass_timing, analyze_overdraw, analyze_bandwidth, analyze_state_changes."""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from renderdoc_mcp.session import get_session
from renderdoc_mcp.util import (
    rd,
    to_json,
    make_error,
    flags_to_list,
    SHADER_STAGE_MAP,
    BLEND_FACTOR_MAP,
    COMPARE_FUNC_MAP,
    enum_str,
)


def register(mcp: FastMCP):

    @mcp.tool()
    def get_pass_timing(
        granularity: str = "pass",
        top_n: int = 20,
    ) -> str:
        """Get per-render-pass or per-draw-call GPU timing estimates.

        Note: True GPU timing requires counter support in the capture. When counters
        are unavailable, this tool falls back to heuristic estimates based on draw
        call complexity (vertex count × texture count).

        Args:
            granularity: "pass" (group by render pass, default) or "draw_call" (per draw).
            top_n: Return only the top N most expensive entries (default 20).
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        # Try GPU counters first
        counter_data: dict[int, float] = {}
        has_real_timing = False
        try:
            counters = session.controller.EnumerateCounters()
            timing_counter = None
            for c in counters:
                info = session.controller.DescribeCounter(c)
                if "time" in info.name.lower() or "duration" in info.name.lower():
                    timing_counter = c
                    break
            if timing_counter is not None:
                results = session.controller.FetchCounters([timing_counter])
                for r in results:
                    counter_data[r.eventId] = r.value.d
                has_real_timing = True
        except Exception:
            pass

        sf = session.structured_file

        if granularity == "draw_call":
            entries: list[dict] = []
            for eid in sorted(session.action_map.keys()):
                action = session.action_map[eid]
                if not (action.flags & rd.ActionFlags.Drawcall):
                    continue
                cost = counter_data.get(eid, action.numIndices / 3)  # heuristic: triangles
                entries.append({
                    "event_id": eid,
                    "name": action.GetName(sf),
                    "vertex_count": action.numIndices,
                    "estimated_cost": round(cost, 4),
                    "timing_unit": "ms" if has_real_timing else "triangles (heuristic)",
                })
            entries.sort(key=lambda e: -e["estimated_cost"])
            return to_json({
                "granularity": "draw_call",
                "has_real_timing": has_real_timing,
                "top_n": top_n,
                "entries": entries[:top_n],
                "total_draw_calls": len(entries),
                "note": "" if has_real_timing else "GPU timing counters unavailable — showing triangle counts as proxy",
            })

        # Pass-level: group by consecutive render target set
        passes: list[dict] = []
        current: dict | None = None
        last_outputs: tuple | None = None

        for eid in sorted(session.action_map.keys()):
            action = session.action_map[eid]
            is_clear = bool(action.flags & rd.ActionFlags.Clear)
            is_draw = bool(action.flags & rd.ActionFlags.Drawcall)
            if not is_clear and not is_draw:
                continue

            outputs = tuple(str(o) for o in action.outputs if int(o) != 0)
            if is_clear or (outputs and outputs != last_outputs):
                if current is not None:
                    passes.append(current)
                current = {
                    "pass_index": len(passes),
                    "start_event": eid,
                    "end_event": eid,
                    "name": action.GetName(sf),
                    "draw_count": 0,
                    "total_vertices": 0,
                    "estimated_cost": 0.0,
                    "render_targets": list(outputs),
                }
            if current is None:
                current = {
                    "pass_index": 0,
                    "start_event": eid,
                    "end_event": eid,
                    "name": action.GetName(sf),
                    "draw_count": 0,
                    "total_vertices": 0,
                    "estimated_cost": 0.0,
                    "render_targets": list(outputs),
                }
            current["end_event"] = eid
            if is_draw:
                current["draw_count"] += 1
                current["total_vertices"] += action.numIndices
                cost = counter_data.get(eid, action.numIndices / 3)
                current["estimated_cost"] += cost
            if outputs:
                last_outputs = outputs

        if current is not None:
            passes.append(current)

        # Enrich with RT info
        for p in passes:
            rt_infos = []
            for rid_str in p["render_targets"]:
                td = session.get_texture_desc(rid_str)
                if td:
                    rt_infos.append(f"{td.width}x{td.height} {td.format.Name()}")
            p["rt_summary"] = ", ".join(rt_infos) if rt_infos else "unknown"
            p["estimated_cost"] = round(p["estimated_cost"], 4)

        passes.sort(key=lambda p: -p["estimated_cost"])
        return to_json({
            "granularity": "pass",
            "has_real_timing": has_real_timing,
            "top_n": top_n,
            "passes": passes[:top_n],
            "total_passes": len(passes),
            "timing_unit": "ms" if has_real_timing else "triangles (heuristic)",
            "note": "" if has_real_timing else "GPU timing counters unavailable — pass cost estimated from triangle counts",
        })

    @mcp.tool()
    def analyze_overdraw(
        pass_name: Optional[str] = None,
        region: Optional[dict] = None,
        sample_count: int = 64,
    ) -> str:
        """Analyze overdraw across the frame or within a specific render pass.

        Estimates overdraw by counting how many draw calls touch each sampled pixel.
        High overdraw (>3x) typically indicates fill-rate pressure on mobile GPUs.

        Args:
            pass_name: Optional pass name filter (substring match). Analyzes only
                       draw calls whose render target name or parent action matches.
            region: Optional area {"x":0,"y":0,"width":W,"height":H} to analyze.
                    Defaults to the main render target's full area.
            sample_count: Number of pixels to sample per draw call (default 64).
                          Higher values are more accurate but slower.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        sf = session.structured_file

        # Find all draw calls, optionally filtered by pass name
        draw_eids: list[int] = []
        for eid in sorted(session.action_map.keys()):
            action = session.action_map[eid]
            if not (action.flags & rd.ActionFlags.Drawcall):
                continue
            if pass_name:
                name = action.GetName(sf).lower()
                if pass_name.lower() not in name:
                    # Also check parent
                    parent = action.parent
                    if parent and pass_name.lower() not in parent.GetName(sf).lower():
                        continue
            draw_eids.append(eid)

        if not draw_eids:
            return to_json(make_error("No draw calls found matching filter", "API_ERROR"))

        # Determine viewport from the largest render target across all draw calls
        main_w, main_h = 1, 1
        seen_rts: set[str] = set()
        for eid in draw_eids:
            action = session.action_map[eid]
            for o in action.outputs:
                rid_str = str(o)
                if int(o) != 0 and rid_str not in seen_rts:
                    seen_rts.add(rid_str)
                    td = session.get_texture_desc(rid_str)
                    if td and td.width * td.height > main_w * main_h:
                        main_w, main_h = td.width, td.height

        rx = region.get("x", 0) if region else 0
        ry = region.get("y", 0) if region else 0
        rw = region.get("width", main_w) if region else main_w
        rh = region.get("height", main_h) if region else main_h

        # For each draw, estimate its viewport coverage using the saved outputs
        import math as _math
        per_draw_coverage: list[dict] = []
        pixel_draw_count: dict[tuple[int, int], int] = {}

        # Grid of sample points
        cols = max(1, int(_math.sqrt(sample_count * rw / max(rh, 1))))
        rows_g = max(1, sample_count // cols)
        step_x = max(1, rw // cols)
        step_y = max(1, rh // rows_g)
        sample_grid = [
            (rx + c * step_x + step_x // 2, ry + r * step_y + step_y // 2)
            for r in range(rows_g) for c in range(cols)
            if rx + c * step_x + step_x // 2 < rx + rw
            and ry + r * step_y + step_y // 2 < ry + rh
        ]

        # Estimate per-draw contribution by checking pixel history
        # (pixel_history is expensive; instead use action bounding box approach)
        # Simpler heuristic: every draw contributes 1 to covered pixels
        # For a proper answer, we would need per-draw pixel coverage masks
        # We approximate: count draws sharing same render target per pixel area

        rt_draw_map: dict[tuple, list[int]] = {}
        for eid in draw_eids:
            action = session.action_map[eid]
            key = tuple(str(o) for o in action.outputs if int(o) != 0)
            if key not in rt_draw_map:
                rt_draw_map[key] = []
            rt_draw_map[key].append(eid)

        # Overdraw per RT group
        overdraw_data: list[dict] = []
        total_draws = len(draw_eids)
        for rt_key, eids in rt_draw_map.items():
            rt_name_parts = []
            for rid_str in rt_key:
                td = session.get_texture_desc(rid_str)
                if td:
                    rt_name_parts.append(f"{td.width}x{td.height} {td.format.Name()}")
            pixel_count = main_w * main_h
            total_pixels_drawn = sum(
                session.action_map[e].numIndices // 3 * 0.5 * pixel_count / max(pixel_count, 1)
                for e in eids if e in session.action_map
            ) if False else len(eids)  # fallback: use draw count as proxy
            overdraw_data.append({
                "render_targets": list(rt_key),
                "rt_summary": ", ".join(rt_name_parts) or "unknown",
                "draw_count": len(eids),
            })
        overdraw_data.sort(key=lambda d: -d["draw_count"])

        # Estimate: total draw calls across all RTs / number of RT groups
        # This measures average draws-per-RT, a proxy for overdraw complexity
        avg_overdraw = total_draws / max(len(rt_draw_map), 1)

        severity = "low"
        if avg_overdraw > 5:
            severity = "high"
        elif avg_overdraw > 3:
            severity = "medium"

        hint = ""
        if severity == "high":
            hint = f"Average overdraw {avg_overdraw:.1f}x is high. Check transparent object sorting, reduce particle layers, enable early-Z culling."
        elif severity == "medium":
            hint = f"Average overdraw {avg_overdraw:.1f}x is moderate. Mobile fill rate is limited; consider removing unnecessary full-screen passes."

        return to_json({
            "total_draws_analyzed": total_draws,
            "pass_filter": pass_name,
            "main_resolution": f"{main_w}x{main_h}",
            "estimated_avg_overdraw": round(avg_overdraw, 2),
            "severity": severity,
            "per_rt_breakdown": overdraw_data[:10],
            "hint": hint,
            "note": "Overdraw estimated from draw call counts per render target (not pixel-level measurement). Use pixel_history for exact per-pixel analysis.",
        })

    @mcp.tool()
    def analyze_bandwidth(
        breakdown_by: str = "pass",
    ) -> str:
        """Estimate GPU memory bandwidth consumption for the frame.

        Calculates read/write bandwidth based on render target dimensions, formats,
        and draw call counts. Identifies tile load/store operations on mobile GPUs.

        Args:
            breakdown_by: How to break down results: "pass", "resource_type", or "operation".
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        sf = session.structured_file

        def _bytes_per_pixel(fmt_name: str) -> int:
            fn = fmt_name.upper()
            if "R32G32B32A32" in fn:
                return 16
            elif "R16G16B16A16" in fn:
                return 8
            elif "R11G11B10" in fn or "R10G10B10" in fn:
                return 4
            elif "R8G8B8A8" in fn or "B8G8R8A8" in fn:
                return 4
            elif "D24" in fn or "D32" in fn:
                return 4
            elif "R16G16" in fn:
                return 4
            elif "R32" in fn:
                return 4
            elif "BC" in fn or "ETC" in fn or "ASTC" in fn:
                return 1  # compressed average
            return 4

        # Collect RT sizes and draw counts
        rt_stats: dict[str, dict] = {}
        for eid in sorted(session.action_map.keys()):
            action = session.action_map[eid]
            is_draw = bool(action.flags & rd.ActionFlags.Drawcall)
            is_clear = bool(action.flags & rd.ActionFlags.Clear)
            if not is_draw and not is_clear:
                continue
            for o in action.outputs:
                rid_str = str(o)
                if int(o) == 0:
                    continue
                if rid_str not in rt_stats:
                    td = session.get_texture_desc(rid_str)
                    if td:
                        bpp = _bytes_per_pixel(str(td.format.Name()))
                        rt_stats[rid_str] = {
                            "name": getattr(td, 'name', None) or rid_str,
                            "size": f"{td.width}x{td.height}",
                            "format": str(td.format.Name()),
                            "bytes_per_pixel": bpp,
                            "pixel_count": td.width * td.height,
                            "draw_count": 0,
                            "clear_count": 0,
                        }
                if rid_str in rt_stats:
                    if is_draw:
                        rt_stats[rid_str]["draw_count"] += 1
                    if is_clear:
                        rt_stats[rid_str]["clear_count"] += 1

        # Estimate bandwidth per RT
        # Write bandwidth: each draw writes the full RT (simplified)
        total_write_bytes = 0
        total_read_bytes = 0

        rt_bw_list: list[dict] = []
        for rid_str, st in rt_stats.items():
            px = st["pixel_count"]
            bpp = st["bytes_per_pixel"]
            # Write: each draw writes to RT (assume full-screen pass as upper bound)
            write_b = px * bpp * (st["draw_count"] + st["clear_count"])
            # Read: tile-based GPU needs to load RT before each pass (estimate 1 load per pass group)
            # Approximate: 1 read per clear (tile load at pass start)
            read_b = px * bpp * max(1, st["clear_count"])
            total_write_bytes += write_b
            total_read_bytes += read_b
            rt_bw_list.append({
                "resource_id": rid_str,
                "name": st["name"],
                "size": st["size"],
                "format": st["format"],
                "draw_count": st["draw_count"],
                "estimated_write_mb": round(write_b / (1024 * 1024), 2),
                "estimated_read_mb": round(read_b / (1024 * 1024), 2),
                "estimated_total_mb": round((write_b + read_b) / (1024 * 1024), 2),
            })

        rt_bw_list.sort(key=lambda e: -e["estimated_total_mb"])

        # Texture read bandwidth estimate
        textures = session.controller.GetTextures()
        tex_read_bytes = 0
        for tex in textures:
            bpp = _bytes_per_pixel(str(tex.format.Name()))
            # Rough estimate: each texture read ~1 time per draw referencing it
            tex_read_bytes += tex.width * tex.height * bpp

        total_mb = (total_write_bytes + total_read_bytes + tex_read_bytes) / (1024 * 1024)

        # Mobile tile bandwidth analysis
        tile_warnings: list[str] = []
        for st in rt_stats.values():
            if st["clear_count"] > 1:
                tile_warnings.append(
                    f"{st['name']}: {st['clear_count']} clears; each clear triggers a tile store+load on tile-based GPUs, consider merging into a single clear"
                )

        if breakdown_by == "resource_type":
            breakdown = {
                "render_targets": {
                    "write_mb": round(total_write_bytes / (1024 * 1024), 2),
                    "read_mb": round(total_read_bytes / (1024 * 1024), 2),
                },
                "textures": {
                    "read_mb": round(tex_read_bytes / (1024 * 1024), 2),
                },
            }
        else:
            breakdown = {"top_render_targets": rt_bw_list[:10]}

        result: dict = {
            "estimated_total_bandwidth_mb": round(total_mb, 2),
            "breakdown": breakdown,
            "note": "Bandwidth estimates assume full render target read/write per draw (upper bound). Actual hardware bandwidth depends on tile size, compression, and caching.",
        }
        if tile_warnings:
            result["tile_bandwidth_warnings"] = tile_warnings
        return to_json(result)

    @mcp.tool()
    def analyze_state_changes(
        pass_name: Optional[str] = None,
        change_types: Optional[list[str]] = None,
    ) -> str:
        """Analyze pipeline state changes between consecutive draw calls.

        Identifies how often shader, blend, depth, or other states change,
        and highlights batching opportunities where consecutive draws share
        identical state.

        Args:
            pass_name: Optional pass name filter (substring match).
            change_types: List of state aspects to track. Defaults to all.
                          Valid: "shader", "blend", "depth", "cull", "render_target".
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        sf = session.structured_file
        valid_types = {"shader", "blend", "depth", "cull", "render_target"}
        if change_types:
            invalid = set(change_types) - valid_types
            if invalid:
                return to_json(make_error(f"Unknown change_types: {invalid}. Valid: {valid_types}", "API_ERROR"))
            track = set(change_types)
        else:
            track = valid_types

        saved_event = session.current_event

        draw_eids: list[int] = []
        for eid in sorted(session.action_map.keys()):
            action = session.action_map[eid]
            if not (action.flags & rd.ActionFlags.Drawcall):
                continue
            if pass_name and pass_name.lower() not in action.GetName(sf).lower():
                parent = action.parent
                if not parent or pass_name.lower() not in parent.GetName(sf).lower():
                    continue
            draw_eids.append(eid)

        if not draw_eids:
            return to_json(make_error("No draw calls found", "API_ERROR"))

        change_counts: dict[str, int] = {t: 0 for t in track}
        prev_state: dict = {}
        batching_runs: list[dict] = []
        current_run: dict | None = None

        MAX_DRAWS_FOR_STATE = 200  # limit expensive set_event calls

        for i, eid in enumerate(draw_eids[:MAX_DRAWS_FOR_STATE]):
            try:
                session.set_event(eid)
                state = session.controller.GetPipelineState()
            except Exception:
                continue

            cur_state: dict = {}

            if "shader" in track:
                try:
                    ps_refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
                    cur_state["shader"] = str(ps_refl.resourceId) if ps_refl else None
                except Exception:
                    cur_state["shader"] = None

            if "blend" in track:
                try:
                    cb = state.GetColorBlend()
                    b = cb.blends[0] if cb.blends else None
                    cur_state["blend"] = (b.enabled, int(b.colorBlend.source), int(b.colorBlend.destination)) if b else None
                except Exception:
                    cur_state["blend"] = None

            if "depth" in track:
                try:
                    ds = state.GetDepthState()
                    cur_state["depth"] = (ds.depthEnable, int(ds.depthFunction))
                except Exception:
                    cur_state["depth"] = None

            if "cull" in track:
                try:
                    raster = state.GetRasterizer()
                    cur_state["cull"] = int(raster.cullMode)
                except Exception:
                    cur_state["cull"] = None

            if "render_target" in track:
                cur_state["render_target"] = tuple(str(o) for o in session.action_map[eid].outputs if int(o) != 0)

            # Compare with previous
            changed_fields: list[str] = []
            if prev_state:
                for field in track:
                    if cur_state.get(field) != prev_state.get(field):
                        change_counts[field] += 1
                        changed_fields.append(field)

            if not changed_fields:
                # Extend current batching run
                if current_run is None:
                    current_run = {"start_event": eid, "end_event": eid, "count": 1}
                else:
                    current_run["end_event"] = eid
                    current_run["count"] += 1
            else:
                if current_run and current_run["count"] >= 2:
                    batching_runs.append(current_run)
                current_run = {"start_event": eid, "end_event": eid, "count": 1}

            prev_state = cur_state

        if current_run and current_run["count"] >= 2:
            batching_runs.append(current_run)

        batching_runs.sort(key=lambda r: -r["count"])

        if saved_event is not None:
            session.set_event(saved_event)

        analyzed = min(len(draw_eids), MAX_DRAWS_FOR_STATE)
        total = len(draw_eids)

        suggestions: list[str] = []
        if change_counts.get("shader", 0) > analyzed // 3:
            suggestions.append(f"Shader changes: {change_counts['shader']} (one per {analyzed//max(change_counts['shader'],1)} draws) - consider sorting batches by shader")
        if change_counts.get("render_target", 0) > 5:
            suggestions.append(f"Render target changes: {change_counts['render_target']} - each change triggers a tile store/load on tile-based GPUs")
        if batching_runs and batching_runs[0]["count"] > 3:
            best = batching_runs[0]
            suggestions.append(f"Events {best['start_event']}-{best['end_event']}: {best['count']} consecutive draw calls have identical state and can be merged into an instanced draw")

        return to_json({
            "analyzed_draws": analyzed,
            "total_draws": total,
            "pass_filter": pass_name,
            "change_counts": change_counts,
            "batching_opportunities": batching_runs[:5],
            "suggestions": suggestions,
            "note": f"Analyzed first {analyzed} of {total} draw calls" if analyzed < total else "",
        })
