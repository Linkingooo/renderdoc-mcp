"""Validation helpers for cross-checking MCP data integrity.

Provides functions to verify that data returned by MCP tools is consistent
and correct by comparing results from multiple independent RenderDoc API calls.
"""

from __future__ import annotations

import math
from typing import Any


def validate_pixel_value(rgba: dict) -> list[str]:
    """Check a pixel RGBA dict for anomalies.

    Returns a list of warning strings (empty if clean).
    """
    warnings: list[str] = []
    for ch in ("r", "g", "b", "a"):
        v = rgba.get(ch)
        if v is None:
            continue
        if isinstance(v, float):
            if math.isnan(v):
                warnings.append(f"channel {ch} is NaN")
            elif math.isinf(v):
                warnings.append(f"channel {ch} is {'+'if v > 0 else '-'}Inf")
    return warnings


def validate_resource_id(resource_id_str: str) -> list[str]:
    """Validate a resource ID string format.

    Returns a list of warning strings (empty if valid).
    """
    warnings: list[str] = []
    if not resource_id_str or resource_id_str == "ResourceId::0":
        warnings.append("resource_id is null/zero — likely unbound or invalid")
    elif not resource_id_str.startswith("ResourceId::"):
        warnings.append(f"unexpected resource_id format: {resource_id_str}")
    return warnings


def validate_event_consistency(
    expected_event: int | None,
    actual_event: int | None,
) -> list[str]:
    """Check that the event context matches expectations.

    Returns a list of warning strings (empty if consistent).
    """
    warnings: list[str] = []
    if expected_event is not None and actual_event is not None:
        if expected_event != actual_event:
            warnings.append(
                f"event mismatch: requested event {expected_event} "
                f"but session reports event {actual_event} — "
                f"data may be from wrong event context"
            )
    elif actual_event is None:
        warnings.append("no event is currently set — data may be undefined")
    return warnings


def validate_texture_dimensions(
    width: int,
    height: int,
    depth: int = 1,
) -> list[str]:
    """Sanity-check texture dimensions.

    Returns a list of warning strings (empty if valid).
    """
    warnings: list[str] = []
    if width <= 0 or height <= 0:
        warnings.append(f"invalid texture size: {width}x{height}")
    if width > 16384 or height > 16384:
        warnings.append(f"unusually large texture: {width}x{height}")
    if depth < 1:
        warnings.append(f"invalid texture depth: {depth}")
    return warnings


def validate_float_array(values: list, context: str = "") -> list[str]:
    """Check a list of float values for NaN, Inf, and suspicious patterns.

    Returns a list of warning strings (empty if clean).
    """
    warnings: list[str] = []
    nan_count = 0
    inf_count = 0
    for v in values:
        if isinstance(v, float):
            if math.isnan(v):
                nan_count += 1
            elif math.isinf(v):
                inf_count += 1
    prefix = f"{context}: " if context else ""
    if nan_count > 0:
        warnings.append(f"{prefix}{nan_count} NaN value(s) detected")
    if inf_count > 0:
        warnings.append(f"{prefix}{inf_count} Inf value(s) detected")
    return warnings


def cross_validate_pixel(
    pick_result: dict,
    read_result: dict,
    tolerance: float = 1e-4,
) -> list[str]:
    """Cross-validate pixel data from pick_pixel vs read_texture_pixels.

    Both results should have RGBA values for the same pixel. Compares them
    within a floating-point tolerance.

    Args:
        pick_result: The "rgba" dict from pick_pixel.
        read_result: A [r, g, b, a] list from read_texture_pixels.
        tolerance: Maximum per-channel difference to accept.

    Returns a list of mismatch descriptions (empty if consistent).
    """
    warnings: list[str] = []

    pick_rgba = pick_result
    if isinstance(read_result, list) and len(read_result) >= 4:
        read_rgba = {"r": read_result[0], "g": read_result[1],
                     "b": read_result[2], "a": read_result[3]}
    elif isinstance(read_result, dict):
        read_rgba = read_result
    else:
        return ["cannot compare: read_result format unrecognized"]

    for ch in ("r", "g", "b", "a"):
        pv = pick_rgba.get(ch)
        rv = read_rgba.get(ch)
        if pv is None or rv is None:
            continue
        if isinstance(pv, float) and isinstance(rv, float):
            if math.isnan(pv) and math.isnan(rv):
                continue  # both NaN — consistent
            if math.isnan(pv) != math.isnan(rv):
                warnings.append(f"channel {ch}: NaN mismatch (pick={pv}, read={rv})")
                continue
            if math.isinf(pv) != math.isinf(rv):
                warnings.append(f"channel {ch}: Inf mismatch (pick={pv}, read={rv})")
                continue
            diff = abs(pv - rv)
            if diff > tolerance:
                warnings.append(
                    f"channel {ch}: value mismatch (pick={pv:.6f}, "
                    f"read={rv:.6f}, diff={diff:.6f})"
                )
    return warnings


def validate_pipeline_state(state: dict) -> list[str]:
    """Validate a pipeline state dict for common issues.

    Returns a list of warning strings (empty if clean).
    """
    warnings: list[str] = []

    # Check for missing shaders
    shaders = state.get("shaders", {})
    if shaders.get("vertex", {}).get("bound") is False:
        warnings.append("no vertex shader bound — draw call will produce no output")
    if shaders.get("pixel", {}).get("bound") is False:
        warnings.append("no pixel shader bound — output color may be undefined")

    # Check for zero-size viewports
    for i, vp in enumerate(state.get("viewports", [])):
        if vp.get("width", 0) == 0 or vp.get("height", 0) == 0:
            warnings.append(f"viewport[{i}] has zero size — nothing will be rendered")

    # Check output targets
    targets = state.get("output_targets", [])
    if not targets:
        warnings.append("no output targets bound — draw call has nowhere to write")

    # Check for null resource IDs in outputs
    for i, t in enumerate(targets):
        rid = t.get("resource_id", "")
        w = validate_resource_id(rid)
        if w:
            warnings.append(f"output_target[{i}]: {w[0]}")

    return warnings


def build_validation_summary(checks: dict[str, list[str]]) -> dict:
    """Build a structured validation summary from check results.

    Args:
        checks: Dict mapping check_name -> list of warning strings.

    Returns a summary dict with pass/fail status and details.
    """
    all_warnings: list[dict] = []
    passed = 0
    failed = 0

    for check_name, warnings in checks.items():
        if warnings:
            failed += 1
            all_warnings.append({
                "check": check_name,
                "status": "WARN",
                "issues": warnings,
            })
        else:
            passed += 1

    return {
        "passed": passed,
        "failed": failed,
        "total_checks": passed + failed,
        "status": "PASS" if failed == 0 else "WARN",
        "issues": all_warnings,
    }
