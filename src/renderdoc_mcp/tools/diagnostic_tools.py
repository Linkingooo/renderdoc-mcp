"""Intelligent diagnostic tools: diagnose_negative_values, diagnose_precision_issues,
diagnose_reflection_mismatch, diagnose_mobile_risks."""

from __future__ import annotations

import math
from typing import Optional

from mcp.server.fastmcp import FastMCP

from renderdoc_mcp.session import get_session
from renderdoc_mcp.util import (
    rd,
    to_json,
    make_error,
    flags_to_list,
    SHADER_STAGE_MAP,
    enum_str,
    COMPARE_FUNC_MAP,
    BLEND_FACTOR_MAP,
    CULL_MODE_MAP,
)


def _pick_rgba(session, tex_id, x: int, y: int, slice_idx: int = 0):
    """Pick pixel and return (r, g, b, a) floats. Returns None on failure."""
    try:
        v = session.controller.PickPixel(
            tex_id, x, y,
            rd.Subresource(0, slice_idx, 0), rd.CompType.Typeless
        )
        return v.floatValue[0], v.floatValue[1], v.floatValue[2], v.floatValue[3]
    except Exception:
        return None


def _sample_rt_for_negatives(session, tex_id, tex_desc, n_samples: int = 128):
    """Sample a render target for negative/NaN/Inf pixels. Returns summary dict."""
    import math as _m
    w, h = tex_desc.width, tex_desc.height
    cols = max(1, int(_m.sqrt(n_samples * w / max(h, 1))))
    rows = max(1, n_samples // cols)
    sx, sy = max(1, w // cols), max(1, h // rows)

    neg_pixels: list[dict] = []
    inf_pixels: list[dict] = []
    nan_pixels: list[dict] = []
    total = 0

    for r in range(rows):
        for c in range(cols):
            px, py = c * sx + sx // 2, r * sy + sy // 2
            if px >= w or py >= h:
                continue
            total += 1
            rgba = _pick_rgba(session, tex_id, px, py)
            if rgba is None:
                continue
            rv, gv, bv, av = rgba
            if any(_m.isnan(v) for v in [rv, gv, bv]):
                nan_pixels.append({"x": px, "y": py, "value": [rv, gv, bv]})
            elif any(_m.isinf(v) for v in [rv, gv, bv]):
                inf_pixels.append({"x": px, "y": py, "value": [
                    "+Inf" if _m.isinf(v) and v > 0 else ("-Inf" if _m.isinf(v) else round(v, 5))
                    for v in [rv, gv, bv]
                ]})
            elif any(v < 0 for v in [rv, gv, bv]):
                neg_pixels.append({"x": px, "y": py, "value": [round(v, 6) for v in [rv, gv, bv]]})

    return {
        "total_samples": total,
        "negative_count": len(neg_pixels),
        "inf_count": len(inf_pixels),
        "nan_count": len(nan_pixels),
        "neg_samples": neg_pixels[:5],
        "inf_samples": inf_pixels[:5],
        "nan_samples": nan_pixels[:5],
    }


def register(mcp: FastMCP):

    @mcp.tool()
    def diagnose_negative_values(
        check_targets: Optional[list[str]] = None,
        trace_depth: int = 5,
    ) -> str:
        """Scan all float render targets for negative/NaN/Inf pixels and trace their origin.

        Automatically identifies which draw calls first introduce negative values,
        checks if TAA/temporal buffers are amplifying them,
        and provides root cause candidates with fix suggestions.

        Args:
            check_targets: Optional list of render target resource IDs to check.
                           If omitted, checks all floating-point render targets.
            trace_depth: How many events to scan backward when tracing the first
                         event that introduced negative values (default 5).
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        # Enumerate float RTs
        textures = session.controller.GetTextures()
        float_rts: list[object] = []
        for tex in textures:
            fmt = str(tex.format.Name()).upper()
            is_float = any(t in fmt for t in ["FLOAT", "R16", "R32", "R11G11B10"])
            is_rt = bool(tex.creationFlags & getattr(rd.TextureCategory, "ColorTarget", 2))
            if not is_float:
                continue
            rid_str = str(tex.resourceId)
            if check_targets and rid_str not in check_targets:
                continue
            if is_rt or check_targets:
                float_rts.append(tex)

        if not float_rts:
            # Fallback: use all float textures
            float_rts = [t for t in textures if any(
                x in str(t.format.Name()).upper() for x in ["FLOAT", "R16G16B16A16"]
            )]

        sf = session.structured_file
        affected: list[dict] = []

        # For each RT, navigate to last draw that writes to it and sample
        rt_draw_map: dict[str, list[int]] = {}
        for eid in sorted(session.action_map.keys()):
            action = session.action_map[eid]
            if not (action.flags & rd.ActionFlags.Drawcall):
                continue
            for o in action.outputs:
                rid_str = str(o)
                if int(o) != 0:
                    rt_draw_map.setdefault(rid_str, []).append(eid)

        for tex in float_rts[:8]:  # limit to avoid very long runs
            rid_str = str(tex.resourceId)
            draws_to_rt = rt_draw_map.get(rid_str, [])
            if not draws_to_rt:
                continue

            # Navigate to last draw to this RT
            last_eid = draws_to_rt[-1]
            err2 = session.set_event(last_eid)
            if err2:
                continue

            scan = _sample_rt_for_negatives(session, tex.resourceId, tex, n_samples=200)
            has_anomaly = scan["negative_count"] + scan["inf_count"] + scan["nan_count"] > 0

            if not has_anomaly:
                continue

            entry: dict = {
                "resource_id": rid_str,
                "name": getattr(tex, 'name', None) or rid_str,
                "format": str(tex.format.Name()),
                "size": f"{tex.width}x{tex.height}",
                "negative_count": scan["negative_count"],
                "inf_count": scan["inf_count"],
                "nan_count": scan["nan_count"],
                "sample_count": scan["total_samples"],
                "negative_rate": f"{scan['negative_count'] / max(scan['total_samples'], 1) * 100:.1f}%",
            }

            # Find first draw that introduces negatives (scan forward from start)
            first_intro: dict | None = None
            for eid in draws_to_rt[:trace_depth]:
                err2 = session.set_event(eid)
                if err2:
                    continue
                sub_scan = _sample_rt_for_negatives(session, tex.resourceId, tex, n_samples=100)
                if sub_scan["negative_count"] + sub_scan["inf_count"] > 0:
                    first_intro = {
                        "event_id": eid,
                        "name": session.action_map[eid].GetName(sf),
                        "new_negative_pixels": sub_scan["negative_count"],
                        "new_inf_pixels": sub_scan["inf_count"],
                    }
                    if sub_scan["neg_samples"]:
                        first_intro["sample"] = sub_scan["neg_samples"][0]
                    break

            if first_intro:
                entry["first_introduced_at"] = first_intro

            # Detect accumulation (TAA-like pattern)
            if scan["negative_count"] > 10 and first_intro:
                upstream_neg = first_intro.get("new_negative_pixels", 0)
                if scan["negative_count"] > upstream_neg * 2 and upstream_neg > 0:
                    ratio = scan["negative_count"] / upstream_neg
                    entry["accumulation_warning"] = (
                        f"⚠️ Negative count ({scan['negative_count']}) vs. the count where negatives first appeared upstream ({upstream_neg}) is {ratio:.1f}x; "
                        "TAA/temporal feedback is likely amplifying negative values over time"
                    )

            affected.append(entry)

        # Build root cause candidates
        candidates: list[dict] = []
        for entry in affected:
            intro = entry.get("first_introduced_at")
            if intro:
                ename = intro.get("name", "")
                if any(k in ename.upper() for k in ["IBL", "SH", "REFLECTION", "LIGHTING"]):
                    candidates.append({
                        "likelihood": "high",
                        "cause": f"IBL/SH lighting computation produces negative values at event {intro['event_id']}",
                        "evidence": f"Negative values first appear in: {ename}",
                        "fix": "Clamp with max(0, result) after SH sampling, or use a non-negative SH basis",
                    })
                if any(k in ename.upper() for k in ["TAA", "TEMPORAL", "HISTORY"]):
                    candidates.append({
                        "likelihood": "high",
                        "cause": "TAA/temporal pass amplified upstream negative values",
                        "evidence": f"Negative count amplified in TAA pass ({ename})",
                        "fix": "In the TAA shader, set the lower bound of the history sample and AABB neighborhood clamp to 0",
                    })
                if entry.get("accumulation_warning"):
                    candidates.append({
                        "likelihood": "high",
                        "cause": "Temporal feedback accumulation amplifies negative values",
                        "evidence": entry["accumulation_warning"],
                        "fix": "Check temporal weight and color clipping logic so negative values do not enter the history buffer",
                    })

        if not candidates and affected:
            candidates.append({
                "likelihood": "medium",
                "cause": "Floating-point precision or the algorithm produces unexpected negative values",
                "evidence": f"Negative values detected in {len(affected)} RT(s)",
                "fix": "Use debug_shader_at_pixel to step through the shader computation at a negative-value pixel",
            })

        status = "NEGATIVE_VALUES_DETECTED" if affected else "NO_ANOMALIES_FOUND"
        return to_json({
            "scan_result": status,
            "affected_targets": affected,
            "root_cause_candidates": candidates,
            "summary": (
                f"Anomalous values detected in {len(affected)} float RT(s)" if affected
                else "No negative/NaN/Inf values detected in any checked float RT"
            ),
        })

    @mcp.tool()
    def diagnose_precision_issues(
        focus: str = "all",
        threshold: float = 0.01,
    ) -> str:
        """Detect floating-point precision issues in the frame.

        Checks: half-float format limitations (R11G11B10 has no sign bit),
        depth buffer precision near far plane, and common precision-sensitive
        format choices.

        Args:
            focus: Which precision aspects to check:
                   "all" (default), "color_precision", "depth_precision", "format_risks".
            threshold: Relative error threshold for reporting (default 0.01 = 1%).
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        issues: list[dict] = []
        textures = session.controller.GetTextures()
        sf = session.structured_file

        if focus in ("all", "format_risks", "color_precision"):
            # Check for R11G11B10 RTs (no sign bit)
            for tex in textures:
                fmt = str(tex.format.Name()).upper()
                if "R11G11B10" in fmt:
                    rid_str = str(tex.resourceId)
                    # Find draw calls that write to it
                    writing_events = []
                    for eid, action in session.action_map.items():
                        if action.flags & rd.ActionFlags.Drawcall:
                            for o in action.outputs:
                                if str(o) == rid_str:
                                    writing_events.append(eid)
                    issues.append({
                        "type": "format_limitation",
                        "severity": "high",
                        "target": f"{getattr(tex, 'name', None) or rid_str} ({fmt}) {tex.width}x{tex.height}",
                        "description": (
                            "R11G11B10_FLOAT has no sign bit and cannot store negative values. "
                            "Writing negative values is clamped to 0 (Adreno) or produces undefined behavior (Mali)."
                        ),
                        "affected_event_count": len(writing_events),
                        "fix": "If upstream computation can produce negative values (IBL SH, HDR tonemapping), clamp to [0, max] before writing this RT, or switch to R16G16B16A16_FLOAT",
                    })

            # Check for half-precision color targets
            for tex in textures:
                fmt = str(tex.format.Name()).upper()
                if "R16G16B16A16_FLOAT" in fmt and tex.width * tex.height > 100:
                    pass  # R16 is fine, just noting

        if focus in ("all", "depth_precision"):
            # Check depth buffers
            depth_textures = [t for t in textures if "D16" in str(t.format.Name()).upper()
                              or "D24" in str(t.format.Name()).upper()
                              or "D32" in str(t.format.Name()).upper()]
            for dtex in depth_textures:
                fmt = str(dtex.format.Name()).upper()
                if "D16" in fmt:
                    issues.append({
                        "type": "depth_precision",
                        "severity": "medium",
                        "target": f"{getattr(dtex, 'name', None) or str(dtex.resourceId)} (D16) {dtex.width}x{dtex.height}",
                        "description": "D16 depth has only 16-bit precision; z-fighting is very likely near the far plane.",
                        "fix": "Use D24_UNORM_S8_UINT or, better, D32_SFLOAT (reversed-Z)",
                    })
                elif "D24" in fmt:
                    issues.append({
                        "type": "depth_precision",
                        "severity": "low",
                        "target": f"{getattr(dtex, 'name', None) or str(dtex.resourceId)} (D24) {dtex.width}x{dtex.height}",
                        "description": "D24 depth buffer loses precision at distance. Use reversed-Z + D32 for large far-plane scenes.",
                        "fix": "Consider reversed-Z or a smaller far plane; this can improve distant precision about 3x",
                    })

        if focus in ("all", "format_risks"):
            # Check for SRGB/linear mismatches (heuristic)
            srgb_textures = sum(1 for t in textures if "SRGB" in str(t.format.Name()).upper())
            linear_textures = sum(1 for t in textures if "UNORM" in str(t.format.Name()).upper() and "SRGB" not in str(t.format.Name()).upper())
            if srgb_textures > 0 and linear_textures > 0:
                issues.append({
                    "type": "gamma_risk",
                    "severity": "low",
                    "description": f"Frame has both SRGB ({srgb_textures}) and linear UNORM ({linear_textures}) textures; keep gamma handling consistent",
                    "fix": "Make sure SRGB textures are linearized automatically when sampled (sampler uses SRGB), and avoid manual gamma correction on linear textures",
                })

        # Summary
        high_count = sum(1 for i in issues if i.get("severity") == "high")
        med_count = sum(1 for i in issues if i.get("severity") == "medium")
        low_count = sum(1 for i in issues if i.get("severity") == "low")

        return to_json({
            "issues_found": len(issues),
            "issues": issues,
            "summary": f"Detected {len(issues)} precision risk(s) ({high_count} high / {med_count} medium / {low_count} low)",
        })

    @mcp.tool()
    def diagnose_reflection_mismatch(
        reflection_pass_hint: Optional[str] = None,
        object_hint: Optional[str] = None,
    ) -> str:
        """Diagnose why a reflection looks different from the original object.

        Automatically identifies reflection passes, pairs them with the original
        draw calls, compares shaders, blend state, and RT format, then quantifies
        the color difference.

        Args:
            reflection_pass_hint: Name hint for the reflection pass
                                  (e.g. "SceneCapture", "Reflection", "SSR", "Mirror").
                                  Auto-detects if omitted.
            object_hint: Name of the object to compare (e.g. "SM_Rock", "Building").
                         Picks the draw call with largest color difference if omitted.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        sf = session.structured_file

        # Find reflection pass
        REFL_KEYWORDS = ["reflection", "scenecapture", "planar", "ssr", "mirror", "reflect"]
        hint = reflection_pass_hint.lower() if reflection_pass_hint else None

        reflection_eids: list[int] = []
        normal_eids: list[int] = []

        for eid, action in sorted(session.action_map.items()):
            if not (action.flags & rd.ActionFlags.Drawcall):
                continue
            name = action.GetName(sf).lower()
            parent_name = action.parent.GetName(sf).lower() if action.parent else ""
            is_reflection = (hint and (hint in name or hint in parent_name)) or \
                            (not hint and any(k in name or k in parent_name for k in REFL_KEYWORDS))
            if is_reflection:
                reflection_eids.append(eid)
            else:
                normal_eids.append(eid)

        if not reflection_eids:
            return to_json({
                "status": "NO_REFLECTION_PASS_FOUND",
                "message": "No reflection pass found. Try the reflection_pass_hint parameter to specify a pass name keyword.",
                "suggestion": "Use list_actions to inspect the frame structure and find the reflection-related pass name",
            })

        # Try to find paired draw calls
        # Heuristic: find normal and reflection draws that render the same object
        # by matching draw names (removing reflection keywords)
        import re as _re

        def _normalize_name(name: str) -> str:
            for k in REFL_KEYWORDS:
                name = name.replace(k, "").replace(k.title(), "")
            return _re.sub(r'\s+', ' ', name).strip().lower()

        normal_map = {_normalize_name(session.action_map[e].GetName(sf)): e for e in normal_eids}
        refl_map = {_normalize_name(session.action_map[e].GetName(sf)): e for e in reflection_eids}

        paired: list[tuple[int, int]] = []
        for key in set(normal_map) & set(refl_map):
            if object_hint is None or object_hint.lower() in key:
                paired.append((normal_map[key], refl_map[key]))

        # Fallback: just pair first few from each group
        if not paired:
            n = min(3, len(normal_eids), len(reflection_eids))
            paired = list(zip(normal_eids[-n:], reflection_eids[:n]))

        if not paired:
            return to_json({
                "status": "NO_PAIRS_FOUND",
                "reflection_event_count": len(reflection_eids),
                "normal_event_count": len(normal_eids),
                "message": "Found the reflection pass but could not pair it with a corresponding normal-render draw call.",
            })

        results: list[dict] = []

        for normal_eid, refl_eid in paired[:3]:
            normal_action = session.action_map[normal_eid]
            refl_action = session.action_map[refl_eid]

            entry: dict = {
                "object": normal_action.GetName(sf),
                "normal_event_id": normal_eid,
                "reflection_event_id": refl_eid,
            }

            # Compare colors by sampling the respective RTs
            def _get_rt_color(eid: int):
                try:
                    action = session.action_map[eid]
                    for o in action.outputs:
                        if int(o) != 0:
                            tid = session.resolve_resource_id(str(o))
                            td = session.get_texture_desc(str(o))
                            if tid and td:
                                session.set_event(eid)
                                rgba = _pick_rgba(session, tid, td.width // 2, td.height // 2)
                                return rgba, str(td.format.Name()), f"{td.width}x{td.height}"
                except Exception:
                    pass
                return None, None, None

            normal_rgba, normal_fmt, normal_size = _get_rt_color(normal_eid)
            refl_rgba, refl_fmt, refl_size = _get_rt_color(refl_eid)

            if normal_rgba and refl_rgba:
                nr, ng, nb = normal_rgba[:3]
                rr, rg, rb = refl_rgba[:3]
                normal_lum = (nr + ng + nb) / 3
                refl_lum = (rr + rg + rb) / 3
                ratio = refl_lum / max(normal_lum, 1e-6)
                entry["color_comparison"] = {
                    "normal_rt": {"format": normal_fmt, "size": normal_size, "sample_color": [round(v, 4) for v in normal_rgba[:3]]},
                    "reflection_rt": {"format": refl_fmt, "size": refl_size, "sample_color": [round(v, 4) for v in refl_rgba[:3]]},
                    "brightness_ratio": round(ratio, 3),
                    "description": f"Reflection is {'darker' if ratio < 0.95 else 'brighter'} than normal rendering by {abs(1-ratio)*100:.0f}%" if abs(1-ratio) > 0.03 else "Brightness is similar",
                }

            # Compare pipeline state
            causes: list[dict] = []

            try:
                session.set_event(normal_eid)
                ns = session.controller.GetPipelineState()
                session.set_event(refl_eid)
                rs = session.controller.GetPipelineState()

                # Shader comparison
                n_ps = ns.GetShaderReflection(rd.ShaderStage.Pixel)
                r_ps = rs.GetShaderReflection(rd.ShaderStage.Pixel)
                if n_ps and r_ps and str(n_ps.resourceId) != str(r_ps.resourceId):
                    causes.append({
                        "factor": "shader_variant",
                        "detail": f"Pixel shader differs: normal={n_ps.entryPoint} refl={r_ps.entryPoint}",
                        "implication": "The reflection pass uses a different shader variant and may skip some lighting computation (e.g. IBL specular)",
                        "fix": "Check whether the reflection pass shader variant includes full lighting, or compare code differences for the REFLECTION=0/1 macro",
                    })

                # Blend comparison
                try:
                    ncb = ns.GetColorBlend()
                    rcb = rs.GetColorBlend()
                    if ncb.blends and rcb.blends:
                        nb0 = ncb.blends[0]
                        rb0 = rcb.blends[0]
                        if nb0.enabled != rb0.enabled:
                            causes.append({
                                "factor": "blend_state",
                                "detail": f"Blend: normal={nb0.enabled} → reflection={rb0.enabled}",
                                "implication": "The reflection pass has a different blend configuration, which may attenuate color by alpha",
                                "fix": f"Confirm whether the reflection pass needs alpha blend; if not, it should match normal rendering",
                            })
                        elif nb0.enabled and rb0.enabled:
                            ns_src = enum_str(nb0.colorBlend.source, BLEND_FACTOR_MAP, "")
                            rs_src = enum_str(rb0.colorBlend.source, BLEND_FACTOR_MAP, "")
                            if ns_src != rs_src:
                                causes.append({
                                    "factor": "blend_src_factor",
                                    "detail": f"Color src factor: {ns_src} → {rs_src}",
                                    "implication": f"Src factor changed from {ns_src} to {rs_src}, which may reduce brightness",
                                    "fix": f"Check whether the reflection pass blend src factor should match normal rendering",
                                })
                except Exception:
                    pass

                # RT format comparison
                if normal_fmt and refl_fmt and normal_fmt != refl_fmt:
                    causes.append({
                        "factor": "render_target_format",
                        "detail": f"RT format: normal={normal_fmt} → reflection={refl_fmt}",
                        "implication": "Different RT formats may affect precision (e.g. R11G11B10 vs R16G16B16A16)",
                        "fix": "Use the same RT format for the reflection pass to avoid precision loss",
                    })

            except Exception as e:
                entry["comparison_error"] = str(e)

            entry["causes"] = causes
            if not causes:
                entry["note"] = "No obvious differences found; use diff_draw_calls for a deeper comparison"

            results.append(entry)

        return to_json({
            "reflection_pass_type": reflection_pass_hint or "auto-detected",
            "reflection_events_found": len(reflection_eids),
            "paired_objects": results,
            "summary": f"Analyzed {len(results)} normal/reflection draw call pair(s)",
        })

    @mcp.tool()
    def diagnose_mobile_risks(
        check_categories: Optional[list[str]] = None,
        severity_filter: str = "all",
    ) -> str:
        """Comprehensive mobile GPU risk assessment for the current frame.

        Checks precision, performance, compatibility, and GPU-specific issues.
        Provides prioritized risk list with fix suggestions.

        Args:
            check_categories: Categories to check. Default: all.
                               Valid: "precision", "performance", "compatibility", "gpu_specific".
            severity_filter: Only return risks of this severity or higher.
                             "all" (default), "medium", "high".
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        valid_cats = {"precision", "performance", "compatibility", "gpu_specific"}
        if check_categories:
            invalid = set(check_categories) - valid_cats
            if invalid:
                return to_json(make_error(f"Unknown categories: {invalid}", "API_ERROR"))
            cats = set(check_categories)
        else:
            cats = valid_cats

        driver_name = session.driver_name
        gpu_upper = driver_name.upper()
        textures = session.controller.GetTextures()
        sf = session.structured_file

        risks: list[dict] = []

        # ── Precision risks ──
        if "precision" in cats:
            # R11G11B10 negative value risk
            for tex in textures:
                fmt = str(tex.format.Name()).upper()
                if "R11G11B10" in fmt:
                    rid_str = str(tex.resourceId)
                    writing_events = [
                        eid for eid, a in session.action_map.items()
                        if (a.flags & rd.ActionFlags.Drawcall) and any(str(o) == rid_str for o in a.outputs)
                    ]
                    if writing_events:
                        # Sample the RT for actual negatives
                        try:
                            session.set_event(writing_events[-1])
                            tex_id = session.resolve_resource_id(rid_str)
                            scan = _sample_rt_for_negatives(session, tex_id, tex, 100) if tex_id else {"negative_count": 0, "nan_count": 0}
                        except Exception:
                            scan = {"negative_count": 0, "nan_count": 0}

                        detail = f"{getattr(tex, 'name', None) or rid_str} uses R11G11B10_FLOAT (no sign bit); {len(writing_events)} draw call(s) write this RT."
                        if scan.get("negative_count", 0) > 0:
                            detail += f" Confirmed {scan['negative_count']} negative sample(s)."
                        risks.append({
                            "category": "precision",
                            "severity": "high",
                            "title": f"R11G11B10_FLOAT negative value storage risk: {getattr(tex, 'name', None) or rid_str}",
                            "detail": detail,
                            "fix": "Clamp output to [0, +inf] before writing this RT, or switch to R16G16B16A16_FLOAT",
                        })

            # Depth precision
            for tex in textures:
                fmt = str(tex.format.Name()).upper()
                if "D16" in fmt:
                    risks.append({
                        "category": "precision",
                        "severity": "high",
                        "title": f"D16 depth precision insufficient: {getattr(tex, 'name', None) or str(tex.resourceId)}",
                        "detail": "16-bit depth buffer has very low precision; z-fighting is pronounced with a large near/far plane ratio",
                        "fix": "Use D24_UNORM_S8_UINT or D32_SFLOAT + reversed-Z",
                    })

        # ── Performance risks ──
        if "performance" in cats:
            # Count full-screen passes (detect via RT size matching main resolution)
            main_res = (0, 0)
            rt_counts: dict[tuple[int, int], int] = {}
            for eid, action in session.action_map.items():
                if action.flags & rd.ActionFlags.Drawcall:
                    for o in action.outputs:
                        if int(o) != 0:
                            td = session.get_texture_desc(str(o))
                            if td:
                                key = (td.width, td.height)
                                rt_counts[key] = rt_counts.get(key, 0) + 1
                                if rt_counts[key] > rt_counts.get(main_res, 0):
                                    main_res = key

            # Count distinct passes (clears) on main RT
            main_clear_count = 0
            for eid, action in session.action_map.items():
                if action.flags & rd.ActionFlags.Clear:
                    for o in action.outputs:
                        if int(o) != 0:
                            td = session.get_texture_desc(str(o))
                            if td and (td.width, td.height) == main_res:
                                main_clear_count += 1

            if main_clear_count > 6:
                risks.append({
                    "category": "performance",
                    "severity": "medium",
                    "title": f"Too many full-screen passes: {main_clear_count} clears",
                    "detail": f"Main RT ({main_res[0]}x{main_res[1]}) is cleared {main_clear_count} times, indicating many full-screen passes; each triggers a tile store/load on tile-based GPUs",
                    "fix": "Merge post-process passes to reduce RT switches",
                })

            # Sampler count check
            max_samplers = 0
            heavy_eid = 0
            for eid in list(session.action_map.keys())[:50]:
                action = session.action_map[eid]
                if not (action.flags & rd.ActionFlags.Drawcall):
                    continue
                try:
                    session.set_event(eid)
                    state = session.controller.GetPipelineState()
                    ps_refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
                    if ps_refl:
                        n = len(ps_refl.readOnlyResources)
                        if n > max_samplers:
                            max_samplers = n
                            heavy_eid = eid
                except Exception:
                    pass

            if max_samplers > 12:
                risks.append({
                    "category": "compatibility",
                    "severity": "medium",
                    "title": f"High texture sampler binding count: {max_samplers} (event {heavy_eid})",
                    "detail": f"Some mobile GPUs limit samplers to 16; this frame binds up to {max_samplers}",
                    "fix": "Merge textures into an atlas or use texture arrays to reduce sampler count",
                })

        # ── Compatibility risks ──
        if "compatibility" in cats:
            # Check for high-res textures
            for tex in textures:
                if tex.width > 4096 or tex.height > 4096:
                    risks.append({
                        "category": "compatibility",
                        "severity": "medium",
                        "title": f"Oversized texture: {getattr(tex, 'name', None) or str(tex.resourceId)} ({tex.width}x{tex.height})",
                        "detail": "Some mobile devices cap texture size at 4096; exceeding it causes rendering errors or black output",
                        "fix": "Lower texture resolution or use texture streaming",
                    })

        # ── GPU-specific risks ──
        if "gpu_specific" in cats:
            if "ADRENO" in gpu_upper:
                risks.append({
                    "category": "gpu_specific",
                    "severity": "medium",
                    "title": "Adreno: mediump float precision below spec",
                    "detail": "Adreno mediump precision is about the FP16 minimum; normal reconstruction and reflection computation may show visible artifacts",
                    "fix": "Use highp or full float precision for normal reconstruction, lighting, and reflection direction computation",
                })
            elif "MALI" in gpu_upper:
                risks.append({
                    "category": "gpu_specific",
                    "severity": "low",
                    "title": "Mali: discard performance impact",
                    "detail": "Heavy use of discard on Mali GPUs disables early-Z and increases overdraw cost",
                    "fix": "Reduce discard in alpha-test; use alpha-to-coverage or a pre-Z pass instead",
                })

        # Filter by severity
        SEVERITY_ORDER = {"high": 2, "medium": 1, "low": 0}
        min_sev = SEVERITY_ORDER.get(severity_filter, 0)
        if severity_filter != "all":
            risks = [r for r in risks if SEVERITY_ORDER.get(r.get("severity", "low"), 0) >= min_sev]

        risks.sort(key=lambda r: -SEVERITY_ORDER.get(r.get("severity", "low"), 0))

        high_n = sum(1 for r in risks if r.get("severity") == "high")
        med_n = sum(1 for r in risks if r.get("severity") == "medium")
        low_n = sum(1 for r in risks if r.get("severity") == "low")

        return to_json({
            "device": driver_name,
            "risks": risks,
            "risk_count": len(risks),
            "summary": f"Found {len(risks)} risk(s) ({high_n} high / {med_n} medium / {low_n} low)."
                       + (f" Highest priority: {risks[0]['title']}" if risks else " No significant risks detected."),
        })
