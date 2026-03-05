# MCP Data Validation Tools

## Background

When MCP tools read GPU frame data from the RenderDoc replay API, incorrect data may be returned due to:

- **Unstable replay**: Inconsistent results when replaying the same event multiple times (driver bugs, residual GPU state)
- **Event navigation errors**: `SetFrameEvent` did not actually switch to the target event, reading data from a different event
- **Type parsing errors**: Incorrect float/int/half type detection, causing values to be misinterpreted
- **Resource ID errors**: Queried the wrong resource, returning unrelated data
- **Stale cache**: Session-cached resource IDs or texture descriptors no longer match the actual state

The validation tools confirm data trustworthiness by reading the same data through **two independent API paths** and comparing results.

## Validation Tools Overview

| Tool | Purpose | Cross-validation Method |
|------|---------|------------------------|
| `validate_capture_integrity` | Full check after opening a capture | Cache vs live query, event navigation, frame boundaries |
| `validate_event_data` | Single event data integrity | Pipeline state + resource ID + RT pixel sampling |
| `cross_validate_pixel` | Pixel value cross-validation | **PickPixel vs GetTextureData (raw byte parsing)** |
| `validate_cbuffer` | Constant buffer cross-validation | **GetCBufferVariableContents vs GetBufferData (raw byte parsing)** |
| `validate_vertex_output` | Vertex data cross-validation | **Double-read comparison + NaN/Inf/degenerate vertex detection** |
| `validate_resource` | Resource ID validity | Cache vs GetResources() cross-check |

## Core Principle: Reading the Same Data via Different API Paths

### Pixel Value Validation (`cross_validate_pixel`)

```
Path A: PickPixel(tex_id, x, y)
        ↓ GPU sampler reads a single pixel
        ↓ Returns floatValue[4]

Path B: GetTextureData(tex_id, subresource)
        ↓ Reads the full raw bytes of the texture
        ↓ Based on format (R8G8B8A8_UNORM / R16G16B16A16_FLOAT / R32G32B32A32_FLOAT etc.)
        ↓ Manually locates (y * width + x) * bpp offset
        ↓ Parses RGBA values
```

The two paths use completely different RenderDoc internal code paths. If results match, the data is trustworthy.

**Supported texture formats (raw byte parsing):**
- `R32G32B32A32_FLOAT` — 16 bytes/pixel
- `R32G32B32_FLOAT` — 12 bytes/pixel
- `R16G16B16A16_FLOAT` — 8 bytes/pixel (half-precision float)
- `R8G8B8A8_UNORM` — 4 bytes/pixel
- `B8G8R8A8_UNORM` — 4 bytes/pixel (BGRA byte order)
- `R11G11B10_FLOAT` — 4 bytes/pixel (packed float)
- `R32_FLOAT` — 4 bytes/pixel (single channel)
- `R16G16_FLOAT` — 4 bytes/pixel

Unsupported formats automatically fall back to double-PickPixel mode.

### Constant Buffer Validation (`validate_cbuffer`)

```
Path A: GetCBufferVariableContents(pipe, shader, stage, entry, index, resource, ...)
        ↓ RenderDoc structured parsing
        ↓ Returns a typed variable tree (names, types, values)

Path B: GetBufferData(cbuffer_resource, offset, size)
        ↓ Reads raw bytes of the cbuffer
        ↓ Interprets as flat float32 array
        ↓ Compares element-by-element with Path A values in declaration order
```

If the structured parse and raw byte parse disagree, the type interpretation may be wrong.

### Vertex Data Validation (`validate_vertex_output`)

```
Path A: GetPostVSData(0, 0, VSOut) → get buffer ID
        ↓ GetBufferData(vertexResourceId, offset, size)
        ↓ Parse as float array

Path B: Re-navigate to the same event (SetFrameEvent)
        ↓ GetPostVSData + GetBufferData again
        ↓ Compare first 10 vertices
```

Additional checks:
- NaN / Inf detection
- Degenerate vertex (0,0,0) detection
- Abnormal W component (W=0) detection
- `action.numIndices` vs `PostVS.numIndices` consistency
- Vertex stride consistency

## Usage

### 1. Check immediately after opening a capture

```
validate_capture_integrity()
```

Confirms the capture file and replay environment are reliable. Checks:
- Whether events in the action map can be correctly navigated
- Whether the resource cache matches live queries
- Whether all texture dimensions are reasonable
- Whether first and last frame events are accessible

### 2. Validate before analyzing critical draw calls

```
validate_event_data(event_id=1234)
```

Confirms the event's data context is correct: pipeline state, shader bindings, resource IDs, RT pixels.

### 3. When suspecting incorrect pixel values

```
cross_validate_pixel(resource_id="ResourceId::4749", x=512, y=384, event_id=1234)
```

Reads the same pixel via two API paths and returns:
- `pick_rgba` — result from PickPixel
- `raw_rgba` — result from GetTextureData raw byte parsing
- `consistent` — whether results match
- `mismatches` — mismatched channels and differences

### 4. When suspecting incorrect cbuffer values

```
validate_cbuffer(stage="pixel", cbuffer_index=0, event_id=1234)
```

Returns:
- `cross_validation` — structured vs raw byte comparison results
- `value_integrity` — NaN/Inf/extreme value detection
- `binding_resource` — cbuffer binding resource ID validity

### 5. When suspecting incorrect vertex data

```
validate_vertex_output(event_id=1234, stage="vsout")
```

Returns:
- `double_read_consistency` — double-read consistency
- `vertex_integrity` — vertex data quality checks
- `action_consistency` — numIndices consistency

## Return Format

All validation tools return a unified JSON format:

```json
{
  "status": "PASS",
  "passed": 4,
  "failed": 0,
  "total_checks": 4,
  "issues": [],
  "event_id": 1234,
  "action_name": "DrawIndexed"
}
```

When issues are found:

```json
{
  "status": "WARN",
  "passed": 3,
  "failed": 1,
  "issues": [
    {
      "check": "cross_validation",
      "status": "WARN",
      "issues": [
        "structured vs raw buffer: 2 value mismatch(es) out of 16 compared"
      ]
    }
  ]
}
```

## Validation Helper Functions (validation.py)

For internal use or custom validation logic:

| Function | Purpose |
|----------|---------|
| `validate_pixel_value(rgba)` | Detect NaN / Inf |
| `validate_resource_id(rid_str)` | Check format and null values |
| `validate_event_consistency(expected, actual)` | Event navigation consistency |
| `validate_texture_dimensions(w, h, d)` | Texture dimension sanity check |
| `validate_float_array(values, context)` | Float array anomaly detection |
| `cross_validate_pixel(pick, read, tolerance)` | Pixel comparison |
| `validate_pipeline_state(state)` | Common pipeline issues |
| `validate_vertex_data(vertices, num_indices, pos_offset)` | Vertex data quality |
| `validate_cbuffer_variables(variables, byte_size)` | Cbuffer variable anomalies |
| `cross_validate_float_arrays(arr1, arr2, tolerance)` | Element-by-element float array comparison |
| `build_validation_summary(checks)` | Build unified check report |

## Tests

```bash
python -m unittest tests.test_validation -v
```

70 unit tests cover all validation functions, including:
- Half-precision float parsing (half → float)
- R11G11B10_FLOAT unpacking
- Raw byte parsing for all supported texture formats
- Structured variable → flat float array extraction
- Edge cases: NaN, Inf, empty data, unsupported formats
