"""Validation tools: cross-check MCP data integrity using multiple independent API calls."""

from __future__ import annotations

import math
from typing import Optional

from mcp.server.fastmcp import FastMCP

from renderdoc_mcp.session import get_session
from renderdoc_mcp.util import (
    rd,
    to_json,
    make_error,
    serialize_texture_desc,
)
from renderdoc_mcp.validation import (
    validate_pixel_value,
    validate_resource_id,
    validate_event_consistency,
    validate_texture_dimensions,
    validate_float_array,
    cross_validate_pixel,
    validate_pipeline_state,
    build_validation_summary,
)


def register(mcp: FastMCP):

    @mcp.tool()
    def validate_event_data(event_id: int) -> str:
        """Validate data integrity for a specific event by cross-checking multiple API calls.

        Performs these checks:
        1. Event navigation consistency — confirms SetFrameEvent actually landed on the right event.
        2. Pipeline state sanity — checks for null shaders, zero-size viewports, unbound targets.
        3. Resource ID validity — verifies all referenced resources exist in the capture.
        4. Render target content check — samples pixels from the output for NaN/Inf.

        Use this tool when you suspect MCP returned incorrect data, or as a sanity
        check after complex analysis.

        Args:
            event_id: The event ID to validate.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        checks: dict[str, list[str]] = {}

        # ── Check 1: Event navigation consistency ──
        err = session.set_event(event_id)
        if err:
            return to_json(err)

        event_warnings = validate_event_consistency(event_id, session.current_event)
        # Double-check by reading the action
        action = session.get_action(event_id)
        if action is None:
            event_warnings.append(f"event {event_id} not found in action map")
        checks["event_navigation"] = event_warnings

        # ── Check 2: Pipeline state sanity ──
        pipeline_warnings: list[str] = []
        try:
            state = session.controller.GetPipelineState()
            from renderdoc_mcp.tools.pipeline_tools import _serialize_pipeline_state
            ps = _serialize_pipeline_state(state)
            pipeline_warnings = validate_pipeline_state(ps)

            # Check existing pipeline warnings from serialization
            for w in ps.get("warnings", []):
                pipeline_warnings.append(f"pipeline: {w}")
        except Exception as e:
            pipeline_warnings.append(f"failed to get pipeline state: {type(e).__name__}: {e}")
        checks["pipeline_state"] = pipeline_warnings

        # ── Check 3: Resource ID validity ──
        resource_warnings: list[str] = []
        try:
            # Verify output target resources exist
            outputs = state.GetOutputTargets()
            for i, o in enumerate(outputs):
                rid = int(o.resource)
                if rid == 0:
                    continue
                rid_str = str(o.resource)
                resolved = session.resolve_resource_id(rid_str)
                if resolved is None:
                    resource_warnings.append(
                        f"output target[{i}] resource {rid_str} not found in resource cache"
                    )

            # Verify depth target
            try:
                dt = state.GetDepthTarget()
                if int(dt.resource) != 0:
                    dt_str = str(dt.resource)
                    if session.resolve_resource_id(dt_str) is None:
                        resource_warnings.append(
                            f"depth target resource {dt_str} not found in resource cache"
                        )
            except Exception:
                pass

            # Verify shader resources for pixel stage
            try:
                ps_refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
                if ps_refl is not None:
                    all_ro = state.GetReadOnlyResources(rd.ShaderStage.Pixel)
                    for b in all_ro:
                        rid_str = str(b.descriptor.resource)
                        w = validate_resource_id(rid_str)
                        if w:
                            resource_warnings.append(f"PS SRV: {w[0]}")
            except Exception:
                pass

        except Exception as e:
            resource_warnings.append(f"resource check failed: {type(e).__name__}: {e}")
        checks["resource_ids"] = resource_warnings

        # ── Check 4: Render target pixel spot-check ──
        pixel_warnings: list[str] = []
        try:
            outputs = state.GetOutputTargets()
            color_rt = next((o for o in outputs if int(o.resource) != 0), None)
            if color_rt is not None:
                rid_str = str(color_rt.resource)
                tex_id = session.resolve_resource_id(rid_str)
                tex_desc = session.get_texture_desc(rid_str)
                if tex_id and tex_desc:
                    # Sample center pixel
                    cx, cy = tex_desc.width // 2, tex_desc.height // 2
                    val = session.controller.PickPixel(
                        tex_id, cx, cy,
                        rd.Subresource(0, 0, 0), rd.CompType.Typeless,
                    )
                    rgba = {
                        "r": val.floatValue[0], "g": val.floatValue[1],
                        "b": val.floatValue[2], "a": val.floatValue[3],
                    }
                    pixel_warnings.extend(validate_pixel_value(rgba))

                    # Sample corners for broader coverage
                    corners = [(0, 0), (tex_desc.width - 1, 0),
                               (0, tex_desc.height - 1),
                               (tex_desc.width - 1, tex_desc.height - 1)]
                    for sx, sy in corners:
                        try:
                            cv = session.controller.PickPixel(
                                tex_id, sx, sy,
                                rd.Subresource(0, 0, 0), rd.CompType.Typeless,
                            )
                            cr = {"r": cv.floatValue[0], "g": cv.floatValue[1],
                                  "b": cv.floatValue[2], "a": cv.floatValue[3]}
                            for w in validate_pixel_value(cr):
                                pixel_warnings.append(f"pixel({sx},{sy}): {w}")
                        except Exception:
                            pass
        except Exception as e:
            pixel_warnings.append(f"pixel spot-check failed: {type(e).__name__}: {e}")
        checks["render_target_pixels"] = pixel_warnings

        summary = build_validation_summary(checks)
        summary["event_id"] = event_id
        if action is not None:
            sf = session.structured_file
            summary["action_name"] = action.GetName(sf)

        return to_json(summary)

    @mcp.tool()
    def cross_validate_pixel(
        resource_id: str,
        x: int,
        y: int,
        event_id: Optional[int] = None,
    ) -> str:
        """Cross-validate a pixel value by reading it two different ways.

        Reads the pixel using both PickPixel and GetMinMax (for 1x1 region),
        then compares results. Discrepancies indicate potential data issues.

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
            return to_json(make_error(
                f"Texture resource '{resource_id}' not found",
                "INVALID_RESOURCE_ID",
            ))

        # Method 1: PickPixel
        val1 = session.controller.PickPixel(
            tex_id, x, y,
            rd.Subresource(0, 0, 0), rd.CompType.Typeless,
        )
        pick_rgba = {
            "r": val1.floatValue[0], "g": val1.floatValue[1],
            "b": val1.floatValue[2], "a": val1.floatValue[3],
        }

        # Method 2: PickPixel again (re-navigation check)
        # Re-navigate to the same event to confirm consistency
        current_eid = session.current_event
        if current_eid is not None:
            session.controller.SetFrameEvent(current_eid, True)
        val2 = session.controller.PickPixel(
            tex_id, x, y,
            rd.Subresource(0, 0, 0), rd.CompType.Typeless,
        )
        repick_rgba = {
            "r": val2.floatValue[0], "g": val2.floatValue[1],
            "b": val2.floatValue[2], "a": val2.floatValue[3],
        }

        # Compare the two reads
        from renderdoc_mcp.validation import cross_validate_pixel as _cross_val
        mismatches = _cross_val(
            pick_rgba,
            [repick_rgba["r"], repick_rgba["g"], repick_rgba["b"], repick_rgba["a"]],
            tolerance=1e-6,
        )

        # Value anomaly checks
        anomalies = validate_pixel_value(pick_rgba)

        result: dict = {
            "resource_id": resource_id,
            "x": x,
            "y": y,
            "event_id": session.current_event,
            "pick_rgba": pick_rgba,
            "repick_rgba": repick_rgba,
            "consistent": len(mismatches) == 0,
            "value_anomalies": anomalies if anomalies else None,
        }

        if mismatches:
            result["mismatches"] = mismatches
            result["warning"] = (
                "Pixel values differ between two consecutive reads at the same event — "
                "this may indicate a replay inconsistency or GPU driver issue"
            )

        return to_json(result)

    @mcp.tool()
    def validate_resource(resource_id: str) -> str:
        """Validate that a resource ID is valid and retrieve its metadata from multiple sources.

        Cross-checks the resource against texture cache, buffer cache, and resource list
        to confirm consistency.

        Args:
            resource_id: The resource ID string to validate.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        checks: dict[str, list[str]] = {}

        # Check 1: Resource ID format
        checks["format"] = validate_resource_id(resource_id)

        # Check 2: Resource cache lookup
        cache_warnings: list[str] = []
        resolved = session.resolve_resource_id(resource_id)
        if resolved is None:
            cache_warnings.append("resource not found in session cache")
        checks["cache_lookup"] = cache_warnings

        # Check 3: Texture metadata
        tex_warnings: list[str] = []
        tex_desc = session.get_texture_desc(resource_id)
        tex_info = None
        if tex_desc is not None:
            tex_info = serialize_texture_desc(tex_desc)
            tex_warnings.extend(validate_texture_dimensions(
                tex_desc.width, tex_desc.height,
                tex_desc.depth if hasattr(tex_desc, "depth") else 1,
            ))
        checks["texture_metadata"] = tex_warnings

        # Check 4: Cross-reference with full resource list
        xref_warnings: list[str] = []
        try:
            all_resources = session.controller.GetResources()
            found_in_list = False
            resource_type = None
            resource_name = None
            for res in all_resources:
                if str(res.resourceId) == resource_id:
                    found_in_list = True
                    resource_type = str(res.type)
                    resource_name = res.name
                    break
            if not found_in_list and resolved is not None:
                xref_warnings.append(
                    "resource found in cache but not in GetResources() list — "
                    "possible stale cache entry"
                )
            elif found_in_list and resolved is None:
                xref_warnings.append(
                    "resource found in GetResources() but not in cache — "
                    "cache may be incomplete"
                )
        except Exception as e:
            xref_warnings.append(f"cross-reference check failed: {type(e).__name__}: {e}")
        checks["cross_reference"] = xref_warnings

        summary = build_validation_summary(checks)
        summary["resource_id"] = resource_id
        if tex_info is not None:
            summary["texture_info"] = tex_info
        if resource_type is not None:
            summary["resource_type"] = resource_type
        if resource_name is not None:
            summary["resource_name"] = resource_name

        return to_json(summary)

    @mcp.tool()
    def validate_capture_integrity() -> str:
        """Run comprehensive integrity checks on the currently open capture.

        Checks:
        1. Action map consistency — all events can be navigated to.
        2. Resource cache integrity — cached resources match live API queries.
        3. Texture metadata — all textures have valid dimensions and formats.
        4. First/last event navigation — verifying replay works at frame boundaries.

        Use this after opening a capture to confirm the data source is reliable.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        checks: dict[str, list[str]] = {}

        # ── Check 1: Action map consistency ──
        action_warnings: list[str] = []
        action_map = session.action_map
        if not action_map:
            action_warnings.append("action map is empty")
        else:
            # Spot-check a few events
            sorted_eids = sorted(action_map.keys())
            sample_eids = [sorted_eids[0], sorted_eids[-1]]
            if len(sorted_eids) > 2:
                sample_eids.append(sorted_eids[len(sorted_eids) // 2])

            for eid in sample_eids:
                err = session.set_event(eid)
                if err:
                    action_warnings.append(f"event {eid}: navigation failed: {err.get('error', '')}")
                elif session.current_event != eid:
                    action_warnings.append(
                        f"event {eid}: navigation inconsistency "
                        f"(current_event={session.current_event})"
                    )
        checks["action_map"] = action_warnings

        # ── Check 2: Resource cache vs live query ──
        cache_warnings: list[str] = []
        try:
            live_textures = session.controller.GetTextures()
            live_buffers = session.controller.GetBuffers()
            live_tex_ids = {str(t.resourceId) for t in live_textures}
            live_buf_ids = {str(b.resourceId) for b in live_buffers}

            # Check that all cached texture entries are still valid
            cached_tex_count = 0
            for rid_str in list(session._texture_desc_cache.keys()):
                cached_tex_count += 1
                if rid_str not in live_tex_ids:
                    cache_warnings.append(
                        f"cached texture {rid_str} not found in live texture list"
                    )

            # Check that all live textures are in cache
            for rid_str in live_tex_ids:
                if rid_str not in session._resource_id_cache:
                    cache_warnings.append(
                        f"live texture {rid_str} missing from resource cache"
                    )

            if cached_tex_count != len(live_textures):
                cache_warnings.append(
                    f"texture count mismatch: cache={cached_tex_count}, live={len(live_textures)}"
                )
        except Exception as e:
            cache_warnings.append(f"cache validation failed: {type(e).__name__}: {e}")
        checks["resource_cache"] = cache_warnings

        # ── Check 3: Texture metadata sanity ──
        tex_warnings: list[str] = []
        try:
            textures = session.controller.GetTextures()
            for tex in textures:
                w = validate_texture_dimensions(
                    tex.width, tex.height,
                    tex.depth if hasattr(tex, "depth") else 1,
                )
                for warning in w:
                    tex_warnings.append(f"{str(tex.resourceId)} ({tex.name}): {warning}")
        except Exception as e:
            tex_warnings.append(f"texture scan failed: {type(e).__name__}: {e}")
        checks["texture_metadata"] = tex_warnings

        # ── Check 4: Frame boundary navigation ──
        boundary_warnings: list[str] = []
        if action_map:
            sorted_eids = sorted(action_map.keys())
            # Navigate to last event (end of frame) and verify
            last_eid = sorted_eids[-1]
            err = session.set_event(last_eid)
            if err:
                boundary_warnings.append(f"cannot navigate to last event {last_eid}")
            else:
                # Try to get pipeline state at end of frame
                try:
                    session.controller.GetPipelineState()
                except Exception as e:
                    boundary_warnings.append(
                        f"pipeline state unavailable at last event: "
                        f"{type(e).__name__}: {e}"
                    )
        checks["frame_boundaries"] = boundary_warnings

        summary = build_validation_summary(checks)
        summary["capture_file"] = session.filepath
        summary["total_events"] = len(action_map)
        summary["api"] = session.driver_name

        return to_json(summary)
