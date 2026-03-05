"""Unit tests for renderdoc_mcp.validation — data integrity checking helpers."""

import math
import struct
import sys
import unittest
from unittest.mock import MagicMock

# Mock the renderdoc module before any imports touch it
mock_rd = MagicMock()


class MockActionFlags:
    NoFlags = 0
    Clear = 1
    Drawcall = 2
    Dispatch = 4
    CmdList = 8
    SetMarker = 16
    PushMarker = 32
    PopMarker = 64
    Present = 128
    MultiAction = 256
    Copy = 512
    Resolve = 1024
    GenMips = 2048
    PassBoundary = 4096
    Indexed = 8192
    Instanced = 16384
    Auto = 32768
    Indirect = 65536
    MeshDispatch = 131072


mock_rd.ActionFlags = MockActionFlags
sys.modules["renderdoc"] = mock_rd
sys.modules["_renderdoc"] = mock_rd

from renderdoc_mcp.validation import (  # noqa: E402
    validate_pixel_value,
    validate_resource_id,
    validate_event_consistency,
    validate_texture_dimensions,
    validate_float_array,
    cross_validate_pixel,
    validate_pipeline_state,
    validate_vertex_data,
    validate_cbuffer_variables,
    cross_validate_float_arrays,
    build_validation_summary,
)


class TestValidatePixelValue(unittest.TestCase):
    def test_clean_pixel(self):
        rgba = {"r": 0.5, "g": 0.3, "b": 0.1, "a": 1.0}
        warnings = validate_pixel_value(rgba)
        self.assertEqual(warnings, [])

    def test_nan_pixel(self):
        rgba = {"r": float("nan"), "g": 0.3, "b": 0.1, "a": 1.0}
        warnings = validate_pixel_value(rgba)
        self.assertEqual(len(warnings), 1)
        self.assertIn("NaN", warnings[0])

    def test_inf_pixel(self):
        rgba = {"r": 0.5, "g": float("inf"), "b": 0.1, "a": 1.0}
        warnings = validate_pixel_value(rgba)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Inf", warnings[0])

    def test_negative_inf(self):
        rgba = {"r": float("-inf"), "g": 0.0, "b": 0.0, "a": 1.0}
        warnings = validate_pixel_value(rgba)
        self.assertEqual(len(warnings), 1)
        self.assertIn("-Inf", warnings[0])

    def test_multiple_anomalies(self):
        rgba = {"r": float("nan"), "g": float("inf"), "b": 0.1, "a": 1.0}
        warnings = validate_pixel_value(rgba)
        self.assertEqual(len(warnings), 2)


class TestValidateResourceId(unittest.TestCase):
    def test_valid_resource_id(self):
        warnings = validate_resource_id("ResourceId::4749")
        self.assertEqual(warnings, [])

    def test_null_resource_id(self):
        warnings = validate_resource_id("ResourceId::0")
        self.assertEqual(len(warnings), 1)
        self.assertIn("null/zero", warnings[0])

    def test_empty_resource_id(self):
        warnings = validate_resource_id("")
        self.assertEqual(len(warnings), 1)

    def test_unexpected_format(self):
        warnings = validate_resource_id("some_random_string")
        self.assertEqual(len(warnings), 1)
        self.assertIn("unexpected", warnings[0])


class TestValidateEventConsistency(unittest.TestCase):
    def test_consistent(self):
        warnings = validate_event_consistency(42, 42)
        self.assertEqual(warnings, [])

    def test_mismatch(self):
        warnings = validate_event_consistency(42, 99)
        self.assertEqual(len(warnings), 1)
        self.assertIn("mismatch", warnings[0])

    def test_no_current_event(self):
        warnings = validate_event_consistency(42, None)
        self.assertEqual(len(warnings), 1)
        self.assertIn("no event", warnings[0])

    def test_both_none(self):
        warnings = validate_event_consistency(None, None)
        self.assertEqual(len(warnings), 1)


class TestValidateTextureDimensions(unittest.TestCase):
    def test_valid_dimensions(self):
        warnings = validate_texture_dimensions(1920, 1080)
        self.assertEqual(warnings, [])

    def test_zero_width(self):
        warnings = validate_texture_dimensions(0, 1080)
        self.assertEqual(len(warnings), 1)
        self.assertIn("invalid", warnings[0])

    def test_oversized(self):
        warnings = validate_texture_dimensions(32768, 32768)
        self.assertEqual(len(warnings), 1)
        self.assertIn("unusually large", warnings[0])

    def test_invalid_depth(self):
        warnings = validate_texture_dimensions(256, 256, 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("depth", warnings[0])


class TestValidateFloatArray(unittest.TestCase):
    def test_clean_array(self):
        warnings = validate_float_array([1.0, 2.0, 3.0])
        self.assertEqual(warnings, [])

    def test_nan_in_array(self):
        warnings = validate_float_array([1.0, float("nan"), 3.0], "test")
        self.assertEqual(len(warnings), 1)
        self.assertIn("NaN", warnings[0])
        self.assertIn("test", warnings[0])

    def test_inf_in_array(self):
        warnings = validate_float_array([float("inf"), 2.0], "buffer")
        self.assertEqual(len(warnings), 1)
        self.assertIn("Inf", warnings[0])

    def test_mixed_anomalies(self):
        warnings = validate_float_array([float("nan"), float("inf")])
        self.assertEqual(len(warnings), 2)

    def test_non_float_ignored(self):
        warnings = validate_float_array([1, "hello", None, 3.0])
        self.assertEqual(warnings, [])


class TestCrossValidatePixel(unittest.TestCase):
    def test_identical_values(self):
        pick = {"r": 0.5, "g": 0.3, "b": 0.1, "a": 1.0}
        read = [0.5, 0.3, 0.1, 1.0]
        warnings = cross_validate_pixel(pick, read)
        self.assertEqual(warnings, [])

    def test_within_tolerance(self):
        pick = {"r": 0.500001, "g": 0.3, "b": 0.1, "a": 1.0}
        read = [0.500002, 0.3, 0.1, 1.0]
        warnings = cross_validate_pixel(pick, read, tolerance=1e-4)
        self.assertEqual(warnings, [])

    def test_mismatch(self):
        pick = {"r": 0.5, "g": 0.3, "b": 0.1, "a": 1.0}
        read = [0.9, 0.3, 0.1, 1.0]
        warnings = cross_validate_pixel(pick, read)
        self.assertEqual(len(warnings), 1)
        self.assertIn("channel r", warnings[0])

    def test_nan_consistency(self):
        pick = {"r": float("nan"), "g": 0.3, "b": 0.1, "a": 1.0}
        read = [float("nan"), 0.3, 0.1, 1.0]
        warnings = cross_validate_pixel(pick, read)
        self.assertEqual(warnings, [])  # both NaN is consistent

    def test_nan_mismatch(self):
        pick = {"r": float("nan"), "g": 0.3, "b": 0.1, "a": 1.0}
        read = [0.5, 0.3, 0.1, 1.0]
        warnings = cross_validate_pixel(pick, read)
        self.assertEqual(len(warnings), 1)
        self.assertIn("NaN mismatch", warnings[0])

    def test_dict_read_format(self):
        pick = {"r": 0.5, "g": 0.3, "b": 0.1, "a": 1.0}
        read = {"r": 0.5, "g": 0.3, "b": 0.1, "a": 1.0}
        warnings = cross_validate_pixel(pick, read)
        self.assertEqual(warnings, [])


class TestValidatePipelineState(unittest.TestCase):
    def test_valid_state(self):
        state = {
            "shaders": {
                "vertex": {"bound": True},
                "pixel": {"bound": True},
            },
            "viewports": [{"width": 1920, "height": 1080}],
            "output_targets": [{"resource_id": "ResourceId::123"}],
        }
        warnings = validate_pipeline_state(state)
        self.assertEqual(warnings, [])

    def test_no_vertex_shader(self):
        state = {
            "shaders": {"vertex": {"bound": False}, "pixel": {"bound": True}},
            "viewports": [{"width": 1920, "height": 1080}],
            "output_targets": [{"resource_id": "ResourceId::123"}],
        }
        warnings = validate_pipeline_state(state)
        self.assertTrue(any("vertex shader" in w for w in warnings))

    def test_zero_viewport(self):
        state = {
            "shaders": {"vertex": {"bound": True}, "pixel": {"bound": True}},
            "viewports": [{"width": 0, "height": 1080}],
            "output_targets": [{"resource_id": "ResourceId::123"}],
        }
        warnings = validate_pipeline_state(state)
        self.assertTrue(any("zero size" in w for w in warnings))

    def test_no_output_targets(self):
        state = {
            "shaders": {"vertex": {"bound": True}, "pixel": {"bound": True}},
            "viewports": [{"width": 1920, "height": 1080}],
            "output_targets": [],
        }
        warnings = validate_pipeline_state(state)
        self.assertTrue(any("no output targets" in w for w in warnings))

    def test_null_output_resource(self):
        state = {
            "shaders": {"vertex": {"bound": True}, "pixel": {"bound": True}},
            "viewports": [{"width": 1920, "height": 1080}],
            "output_targets": [{"resource_id": "ResourceId::0"}],
        }
        warnings = validate_pipeline_state(state)
        self.assertTrue(any("null/zero" in w for w in warnings))


class TestValidateVertexData(unittest.TestCase):
    def test_clean_vertices(self):
        vertices = [
            [0.5, 0.3, 0.1, 1.0, 0.0, 1.0],
            [1.0, 0.0, 0.5, 1.0, 0.5, 0.5],
            [0.0, 1.0, 0.0, 1.0, 1.0, 0.0],
        ]
        warnings = validate_vertex_data(vertices)
        self.assertEqual(warnings, [])

    def test_empty_vertices(self):
        warnings = validate_vertex_data([])
        self.assertEqual(len(warnings), 1)
        self.assertIn("empty", warnings[0])

    def test_nan_in_vertices(self):
        vertices = [
            [float("nan"), 0.3, 0.1, 1.0],
            [1.0, 0.0, 0.5, 1.0],
        ]
        warnings = validate_vertex_data(vertices)
        self.assertTrue(any("NaN" in w for w in warnings))

    def test_inf_in_vertices(self):
        vertices = [
            [float("inf"), 0.3, 0.1, 1.0],
            [1.0, 0.0, 0.5, 1.0],
        ]
        warnings = validate_vertex_data(vertices)
        self.assertTrue(any("Inf" in w for w in warnings))

    def test_inconsistent_stride(self):
        vertices = [
            [1.0, 2.0, 3.0],
            [1.0, 2.0, 3.0, 4.0],  # different length
        ]
        warnings = validate_vertex_data(vertices)
        self.assertTrue(any("inconsistent" in w for w in warnings))

    def test_degenerate_positions(self):
        # All vertices at origin — suspicious
        vertices = [
            [0.0, 0.0, 0.0, 1.0],
            [0.0, 0.0, 0.0, 1.0],
            [0.0, 0.0, 0.0, 1.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        warnings = validate_vertex_data(vertices, position_offset=0)
        self.assertTrue(any("(0,0,0)" in w for w in warnings))

    def test_abnormal_w_component(self):
        vertices = [
            [1.0, 2.0, 3.0, 0.0],  # W=0
            [4.0, 5.0, 6.0, 0.0],
            [7.0, 8.0, 9.0, 0.0],
        ]
        warnings = validate_vertex_data(vertices, position_offset=0)
        self.assertTrue(any("W=0" in w for w in warnings))


class TestValidateCbufferVariables(unittest.TestCase):
    def test_clean_variables(self):
        variables = [
            {"name": "color", "value": [1.0, 0.5, 0.3, 1.0], "rows": 1, "columns": 4},
            {"name": "scale", "value": [2.0], "rows": 1, "columns": 1},
        ]
        warnings = validate_cbuffer_variables(variables)
        self.assertEqual(warnings, [])

    def test_empty_variables(self):
        warnings = validate_cbuffer_variables([])
        self.assertEqual(len(warnings), 1)
        self.assertIn("no variables", warnings[0])

    def test_nan_in_variable(self):
        variables = [
            {"name": "badColor", "value": [float("nan"), 0.5, 0.3, 1.0]},
        ]
        warnings = validate_cbuffer_variables(variables)
        self.assertTrue(any("NaN" in w for w in warnings))
        self.assertTrue(any("badColor" in w for w in warnings))

    def test_inf_in_variable(self):
        variables = [
            {"name": "brightness", "value": [float("inf")]},
        ]
        warnings = validate_cbuffer_variables(variables)
        self.assertTrue(any("Inf" in w for w in warnings))

    def test_extremely_large_value(self):
        variables = [
            {"name": "suspiciousVal", "value": [1e35]},
        ]
        warnings = validate_cbuffer_variables(variables)
        self.assertTrue(any("extremely large" in w for w in warnings))

    def test_nested_struct_nan(self):
        variables = [
            {
                "name": "params",
                "members": [
                    {"name": "inner", "value": [float("nan"), 1.0]},
                ],
            },
        ]
        warnings = validate_cbuffer_variables(variables)
        self.assertTrue(any("NaN" in w for w in warnings))
        self.assertTrue(any("inner" in w for w in warnings))

    def test_matrix_values(self):
        # 4x4 matrix as nested list
        variables = [
            {
                "name": "viewProj",
                "value": [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ],
                "rows": 4,
                "columns": 4,
            },
        ]
        warnings = validate_cbuffer_variables(variables)
        self.assertEqual(warnings, [])


class TestCrossValidateFloatArrays(unittest.TestCase):
    def test_identical(self):
        warnings = cross_validate_float_arrays(
            [1.0, 2.0, 3.0], [1.0, 2.0, 3.0]
        )
        self.assertEqual(warnings, [])

    def test_within_tolerance(self):
        warnings = cross_validate_float_arrays(
            [1.0, 2.0], [1.00005, 2.00003], tolerance=1e-4
        )
        self.assertEqual(warnings, [])

    def test_mismatch(self):
        warnings = cross_validate_float_arrays(
            [1.0, 2.0], [1.0, 9.0], context="test"
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("test", warnings[0])
        self.assertIn("mismatched", warnings[0])

    def test_length_mismatch(self):
        warnings = cross_validate_float_arrays([1.0, 2.0], [1.0])
        self.assertEqual(len(warnings), 1)
        self.assertIn("length mismatch", warnings[0])

    def test_nan_consistency(self):
        warnings = cross_validate_float_arrays(
            [float("nan"), 2.0], [float("nan"), 2.0]
        )
        self.assertEqual(warnings, [])

    def test_nan_mismatch(self):
        warnings = cross_validate_float_arrays(
            [float("nan"), 2.0], [1.0, 2.0]
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("NaN mismatch", warnings[0])


class TestBuildValidationSummary(unittest.TestCase):
    def test_all_pass(self):
        checks = {
            "check1": [],
            "check2": [],
        }
        summary = build_validation_summary(checks)
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["issues"], [])

    def test_some_fail(self):
        checks = {
            "check1": [],
            "check2": ["something wrong"],
        }
        summary = build_validation_summary(checks)
        self.assertEqual(summary["status"], "WARN")
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(summary["issues"]), 1)
        self.assertEqual(summary["issues"][0]["check"], "check2")
        self.assertIn("something wrong", summary["issues"][0]["issues"])

    def test_all_fail(self):
        checks = {
            "check1": ["issue1"],
            "check2": ["issue2", "issue3"],
        }
        summary = build_validation_summary(checks)
        self.assertEqual(summary["status"], "WARN")
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["total_checks"], 2)


class TestHalfToFloat(unittest.TestCase):
    """Test the half-precision float parser used in raw texture cross-validation."""

    def test_zero(self):
        from renderdoc_mcp.tools.validation_tools import _half_to_float
        self.assertEqual(_half_to_float(0x0000), 0.0)

    def test_one(self):
        from renderdoc_mcp.tools.validation_tools import _half_to_float
        self.assertAlmostEqual(_half_to_float(0x3C00), 1.0, places=5)

    def test_negative_one(self):
        from renderdoc_mcp.tools.validation_tools import _half_to_float
        self.assertAlmostEqual(_half_to_float(0xBC00), -1.0, places=5)

    def test_inf(self):
        from renderdoc_mcp.tools.validation_tools import _half_to_float
        self.assertEqual(_half_to_float(0x7C00), float("inf"))

    def test_neg_inf(self):
        from renderdoc_mcp.tools.validation_tools import _half_to_float
        self.assertEqual(_half_to_float(0xFC00), float("-inf"))

    def test_nan(self):
        from renderdoc_mcp.tools.validation_tools import _half_to_float
        self.assertTrue(math.isnan(_half_to_float(0x7C01)))

    def test_half(self):
        from renderdoc_mcp.tools.validation_tools import _half_to_float
        self.assertAlmostEqual(_half_to_float(0x3800), 0.5, places=5)


class TestParsePixelFromRaw(unittest.TestCase):
    """Test raw texture pixel parsing for cross-validation."""

    def test_r32g32b32a32_float(self):
        from renderdoc_mcp.tools.validation_tools import _parse_pixel_from_raw
        # 2x1 texture, pixel at (1, 0)
        pixel0 = struct.pack("<ffff", 0.1, 0.2, 0.3, 1.0)
        pixel1 = struct.pack("<ffff", 0.5, 0.6, 0.7, 0.8)
        raw = pixel0 + pixel1
        result = _parse_pixel_from_raw(raw, 1, 0, 2, "R32G32B32A32_FLOAT")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["r"], 0.5, places=5)
        self.assertAlmostEqual(result["g"], 0.6, places=5)
        self.assertAlmostEqual(result["b"], 0.7, places=5)
        self.assertAlmostEqual(result["a"], 0.8, places=5)

    def test_r8g8b8a8_unorm(self):
        from renderdoc_mcp.tools.validation_tools import _parse_pixel_from_raw
        # 1x1 texture: RGBA = (255, 128, 0, 255)
        raw = struct.pack("<BBBB", 255, 128, 0, 255)
        result = _parse_pixel_from_raw(raw, 0, 0, 1, "R8G8B8A8_UNORM")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["r"], 1.0, places=2)
        self.assertAlmostEqual(result["g"], 128 / 255.0, places=2)
        self.assertAlmostEqual(result["b"], 0.0, places=2)
        self.assertAlmostEqual(result["a"], 1.0, places=2)

    def test_b8g8r8a8_unorm(self):
        from renderdoc_mcp.tools.validation_tools import _parse_pixel_from_raw
        # BGRA order: B=0, G=128, R=255, A=255
        raw = struct.pack("<BBBB", 0, 128, 255, 255)
        result = _parse_pixel_from_raw(raw, 0, 0, 1, "B8G8R8A8_UNORM")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["r"], 1.0, places=2)
        self.assertAlmostEqual(result["g"], 128 / 255.0, places=2)
        self.assertAlmostEqual(result["b"], 0.0, places=2)

    def test_unsupported_format(self):
        from renderdoc_mcp.tools.validation_tools import _parse_pixel_from_raw
        result = _parse_pixel_from_raw(b"\x00" * 16, 0, 0, 1, "BC7_UNORM")
        self.assertIsNone(result)


class TestExtractLeafFloats(unittest.TestCase):
    """Test structured variable -> flat float extraction for cbuffer cross-validation."""

    def test_simple_variables(self):
        from renderdoc_mcp.tools.validation_tools import _extract_leaf_floats
        variables = [
            {"name": "color", "value": [1.0, 0.5, 0.3, 1.0]},
            {"name": "scale", "value": [2.0]},
        ]
        result = _extract_leaf_floats(variables)
        self.assertEqual(result, [1.0, 0.5, 0.3, 1.0, 2.0])

    def test_nested_struct(self):
        from renderdoc_mcp.tools.validation_tools import _extract_leaf_floats
        variables = [
            {
                "name": "params",
                "members": [
                    {"name": "x", "value": [1.0]},
                    {"name": "y", "value": [2.0]},
                ],
            },
        ]
        result = _extract_leaf_floats(variables)
        self.assertEqual(result, [1.0, 2.0])

    def test_matrix(self):
        from renderdoc_mcp.tools.validation_tools import _extract_leaf_floats
        variables = [
            {
                "name": "mat",
                "value": [[1.0, 0.0], [0.0, 1.0]],
            },
        ]
        result = _extract_leaf_floats(variables)
        self.assertEqual(result, [1.0, 0.0, 0.0, 1.0])


if __name__ == "__main__":
    unittest.main()
