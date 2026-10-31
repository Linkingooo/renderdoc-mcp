"""Intelligent diagnostic tools: diagnose_negative_values, diagnose_precision_issues,
diagnose_reflection_mismatch, diagnose_mobile_risks."""

from __future__ import annotations

import math
from typing import Optional

from mcp.server.mcpserver import MCPServer

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


def register(mcp: MCPServer):

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
                        f"⚠️ 负值数 ({scan['negative_count']}) 是上游首次引入值 ({upstream_neg}) 的 {ratio:.1f}x，"
                        "疑似 TAA/temporal feedback 正在持续放大负值"
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
                        "likelihood": "高",
                        "cause": f"IBL/SH 光照计算在 event {intro['event_id']} 产生负值",
                        "evidence": f"负值首次出现于: {ename}",
                        "fix": "在 SH 采样后添加 max(0, result) clamp，或使用非负 SH 基函数",
                    })
                if any(k in ename.upper() for k in ["TAA", "TEMPORAL", "HISTORY"]):
                    candidates.append({
                        "likelihood": "高",
                        "cause": "TAA/temporal pass 放大了上游负值",
                        "evidence": f"TAA pass ({ename}) 中负值数量放大",
                        "fix": "TAA shader 中对 history 采样结果和 AABB neighborhood clamp 下界设为 0",
                    })
                if entry.get("accumulation_warning"):
                    candidates.append({
                        "likelihood": "高",
                        "cause": "Temporal feedback 累积放大负值",
                        "evidence": entry["accumulation_warning"],
                        "fix": "检查 temporal weight 和 color clipping 逻辑，确保负值不进入 history buffer",
                    })

        if not candidates and affected:
            candidates.append({
                "likelihood": "中",
                "cause": "浮点精度或算法产生意外负值",
                "evidence": f"在 {len(affected)} 个 RT 中检测到负值",
                "fix": "使用 debug_shader_at_pixel 在负值像素处单步追踪 shader 计算过程",
            })

        status = "NEGATIVE_VALUES_DETECTED" if affected else "NO_ANOMALIES_FOUND"
        return to_json({
            "scan_result": status,
            "affected_targets": affected,
            "root_cause_candidates": candidates,
            "summary": (
                f"在 {len(affected)} 个浮点 RT 中检测到异常值" if affected
                else "所有检查的浮点 RT 均未检测到负值/NaN/Inf"
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
                            "R11G11B10_FLOAT 没有符号位，无法存储负值。"
                            "写入负值会被 clamp 到 0（Adreno）或产生未定义行为（Mali）。"
                        ),
                        "affected_event_count": len(writing_events),
                        "fix": "如上游计算可能产生负值（IBL SH、HDR tonemapping），在写入该 RT 前 clamp 到 [0, max]，或改用 R16G16B16A16_FLOAT",
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
                        "description": "D16 深度精度仅 16-bit，在远平面附近极易出现 z-fighting。",
                        "fix": "改用 D24_UNORM_S8_UINT 或更好的 D32_SFLOAT (reversed-Z)",
                    })
                elif "D24" in fmt:
                    issues.append({
                        "type": "depth_precision",
                        "severity": "low",
                        "target": f"{getattr(dtex, 'name', None) or str(dtex.resourceId)} (D24) {dtex.width}x{dtex.height}",
                        "description": "D24 深度 buffer 在远处精度降低。large far-plane 场景建议使用 reversed-Z + D32。",
                        "fix": "考虑 reversed-Z 技术或缩小 far plane，可将远处精度提升约 3 倍",
                    })

        if focus in ("all", "format_risks"):
            # Check for SRGB/linear mismatches (heuristic)
            srgb_textures = sum(1 for t in textures if "SRGB" in str(t.format.Name()).upper())
            linear_textures = sum(1 for t in textures if "UNORM" in str(t.format.Name()).upper() and "SRGB" not in str(t.format.Name()).upper())
            if srgb_textures > 0 and linear_textures > 0:
                issues.append({
                    "type": "gamma_risk",
                    "severity": "low",
                    "description": f"帧中同时存在 SRGB ({srgb_textures} 个) 和 linear UNORM ({linear_textures} 个) 纹理，注意 gamma 处理一致性",
                    "fix": "确保 SRGB 纹理采样时自动 linearize (sampler 使用 SRGB)，避免在 linear 纹理上手动做 gamma 矫正",
                })

        # Summary
        high_count = sum(1 for i in issues if i.get("severity") == "high")
        med_count = sum(1 for i in issues if i.get("severity") == "medium")
        low_count = sum(1 for i in issues if i.get("severity") == "low")

        return to_json({
            "issues_found": len(issues),
            "issues": issues,
            "summary": f"检测到 {len(issues)} 个精度风险（{high_count} high / {med_count} medium / {low_count} low）",
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
                "message": "未找到反射 pass。尝试使用 reflection_pass_hint 参数指定 pass 名称关键字。",
                "suggestion": "使用 list_actions 查看帧结构，找到反射相关的 pass 名称",
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
                "message": "找到反射 pass 但无法配对对应的正常渲染 draw call。",
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
                    "description": f"反射比正常渲染{'暗' if ratio < 0.95 else '亮'} {abs(1-ratio)*100:.0f}%" if abs(1-ratio) > 0.03 else "亮度接近",
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
                        "detail": f"Pixel shader 不同: normal={n_ps.entryPoint} refl={r_ps.entryPoint}",
                        "implication": "反射 pass 使用了不同的 shader 变体，可能跳过了部分光照计算（如 IBL specular）",
                        "fix": "检查反射 pass shader 变体是否包含完整光照，或对比 REFLECTION=0/1 宏的代码差异",
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
                                "implication": "反射 pass 的 blend 配置不同，可能导致颜色被 alpha 衰减",
                                "fix": f"确认反射 pass 是否需要 alpha blend；如不需要应与正常渲染一致",
                            })
                        elif nb0.enabled and rb0.enabled:
                            ns_src = enum_str(nb0.colorBlend.source, BLEND_FACTOR_MAP, "")
                            rs_src = enum_str(rb0.colorBlend.source, BLEND_FACTOR_MAP, "")
                            if ns_src != rs_src:
                                causes.append({
                                    "factor": "blend_src_factor",
                                    "detail": f"Color src factor: {ns_src} → {rs_src}",
                                    "implication": f"Src factor 从 {ns_src} 变为 {rs_src}，可能导致亮度衰减",
                                    "fix": f"检查反射 pass 的 blend src factor 是否需要与正常渲染一致",
                                })
                except Exception:
                    pass

                # RT format comparison
                if normal_fmt and refl_fmt and normal_fmt != refl_fmt:
                    causes.append({
                        "factor": "render_target_format",
                        "detail": f"RT format: normal={normal_fmt} → reflection={refl_fmt}",
                        "implication": "RT 格式不同可能影响精度（如 R11G11B10 vs R16G16B16A16）",
                        "fix": "统一反射 RT 格式以避免精度损失",
                    })

            except Exception as e:
                entry["comparison_error"] = str(e)

            entry["causes"] = causes
            if not causes:
                entry["note"] = "未发现明显差异，建议使用 diff_draw_calls 进行深度对比"

            results.append(entry)

        return to_json({
            "reflection_pass_type": reflection_pass_hint or "auto-detected",
            "reflection_events_found": len(reflection_eids),
            "paired_objects": results,
            "summary": f"分析了 {len(results)} 对正常/反射 draw call",
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

                        detail = f"{getattr(tex, 'name', None) or rid_str} 使用 R11G11B10_FLOAT（无符号位），{len(writing_events)} 个 draw call 写入该 RT。"
                        if scan.get("negative_count", 0) > 0:
                            detail += f" 已确认检测到 {scan['negative_count']} 个负值样本。"
                        risks.append({
                            "category": "precision",
                            "severity": "high",
                            "title": f"R11G11B10_FLOAT 存储负值风险: {getattr(tex, 'name', None) or rid_str}",
                            "detail": detail,
                            "fix": "在写入该 RT 前 clamp 输出到 [0, +inf]，或改用 R16G16B16A16_FLOAT",
                        })

            # Depth precision
            for tex in textures:
                fmt = str(tex.format.Name()).upper()
                if "D16" in fmt:
                    risks.append({
                        "category": "precision",
                        "severity": "high",
                        "title": f"D16 深度精度不足: {getattr(tex, 'name', None) or str(tex.resourceId)}",
                        "detail": "16-bit 深度 buffer 精度极低，近远平面比例大时 z-fighting 明显",
                        "fix": "改用 D24_UNORM_S8_UINT 或 D32_SFLOAT + reversed-Z",
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
                    "title": f"全屏 pass 过多: {main_clear_count} 次 clear",
                    "detail": f"主 RT ({main_res[0]}x{main_res[1]}) 被 clear {main_clear_count} 次，说明有大量全屏 pass，每次在 tile-based GPU 上触发 tile store/load",
                    "fix": "合并 post-process pass，减少 RT 切换次数",
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
                    "title": f"纹理采样器绑定数偏高: {max_samplers} 个 (event {heavy_eid})",
                    "detail": f"部分移动 GPU 限制最多 16 个采样器，当前最大绑定 {max_samplers} 个",
                    "fix": "合并纹理 atlas 或使用纹理数组减少采样器数量",
                })

        # ── Compatibility risks ──
        if "compatibility" in cats:
            # Check for high-res textures
            for tex in textures:
                if tex.width > 4096 or tex.height > 4096:
                    risks.append({
                        "category": "compatibility",
                        "severity": "medium",
                        "title": f"超大纹理: {getattr(tex, 'name', None) or str(tex.resourceId)} ({tex.width}x{tex.height})",
                        "detail": "部分移动设备最大纹理尺寸为 4096，超过此限制会导致渲染错误或黑图",
                        "fix": "降低纹理分辨率或使用纹理流加载",
                    })

        # ── GPU-specific risks ──
        if "gpu_specific" in cats:
            if "ADRENO" in gpu_upper:
                risks.append({
                    "category": "gpu_specific",
                    "severity": "medium",
                    "title": "Adreno: mediump float 精度低于 spec",
                    "detail": "Adreno GPU 的 mediump 精度约为 FP16 最低要求，法线重建和反射计算可能产生可见瑕疵",
                    "fix": "法线重建、lighting、反射方向计算使用 highp 或 full float precision",
                })
            elif "MALI" in gpu_upper:
                risks.append({
                    "category": "gpu_specific",
                    "severity": "low",
                    "title": "Mali: discard 性能影响",
                    "detail": "Mali GPU 使用大量 discard 会导致 early-Z 失效，增加 overdraw 开销",
                    "fix": "减少 alpha-test 中的 discard 用法，改用 alpha-to-coverage 或 pre-Z pass",
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
            "summary": f"发现 {len(risks)} 个风险项（{high_n} high / {med_n} medium / {low_n} low）。"
                       + (f" 最高优先级：{risks[0]['title']}" if risks else " 未检测到明显风险。"),
        })
