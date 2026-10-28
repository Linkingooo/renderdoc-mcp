# RenderDoc MCP Server Tool Design

> Goal: let Claude Code connect to RenderDoc over MCP for full rendering bug triage, performance analysis, and optimization advice.
> Covers typical scenarios in mobile FPS projects: precision issues, bright flashes, color shifts, negative TAA values, IBL anomalies.

---

## Overall Architecture

```
Claude Code
    │
    └── RenderDoc MCP Server ──── RenderDoc Python API (renderdoc / qrenderdoc module)
            │
            ├── 1. Capture file management (load, device info)
            ├── 2. Event and draw call browsing (list, detail, search)
            ├── 3. Deep pipeline state queries (full blend/depth/raster state + two-event comparison)
            ├── 4. Shader analysis (source, constants, single-pixel debugging)
            ├── 5. Pixel-level diagnosis (pixel history, region scan anomaly detection)
            ├── 6. Resource inspection (texture stats/reads, buffer reads)
            ├── 7. Performance analysis (pass timing, overdraw, bandwidth, state changes)
            └── 8. Smart diagnostics (negative value scan, precision detection, reflection comparison, mobile risk pre-check)
```

Core design principles:

- **LLM readability first**: return values include human-readable summaries and diagnostic hints, not just raw data
- **Built-in anomaly detection**: tools automatically flag NaN / Inf / negative / extreme values.
  The LLM need not judge them itself.
- **Clear operation chain**: each tool's return value includes the IDs and suggested actions for the next step
- **Controlled token budget**: tools with large outputs provide a brief mode and filter parameters.
  This avoids overflowing the context.

---

## Module 1: Capture File Management

---

### 1.1 `load_capture`

Load a RenderDoc capture file.
The prerequisite for all subsequent operations.

```json
{
  "name": "load_capture",
  "description": "Load a RenderDoc capture file (.rdc). This is the prerequisite for all analysis operations; a capture must be loaded before it can be queried. On success, returns a basic overview of the frame. If another file was loaded earlier, it is closed automatically.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "file_path": {
        "type": "string",
        "description": "Absolute path to the .rdc file"
      }
    },
    "required": ["file_path"]
  }
}
```

**Return value**:
```json
{
  "status": "loaded",
  "api_type": "Vulkan",
  "total_events": 3847,
  "total_draw_calls": 1523,
  "total_dispatches": 47,
  "render_targets_count": 12,
  "frame_duration_ms": 16.7,
  "summary": "Vulkan frame, 3847 events (1523 draw / 47 dispatch), frame duration 16.7ms"
}
```

---

### 1.2 `get_capture_info`

Get device and environment information for the currently loaded capture.

```json
{
  "name": "get_capture_info",
  "description": "Get device environment information for the current capture: GPU model, driver version, API version, resolution, RT formats, etc. When triaging mobile precision / compatibility bugs, device information is a key clue: half float behavior differs greatly between Adreno and Mali, and many bright-flash bugs are strongly GPU-specific.",
  "inputSchema": {
    "type": "object",
    "properties": {},
    "required": []
  }
}
```

**Return value**:
```json
{
  "gpu": "Adreno 640",
  "driver_version": "512.502.0",
  "api": "Vulkan 1.1",
  "resolution": "2400x1080",
  "main_color_format": "R11G11B10_FLOAT",
  "depth_format": "D24_UNORM_S8_UINT",
  "device_type": "mobile",
  "known_gpu_quirks": [
    "Adreno: mediump float precision may actually be lower than the spec requires",
    "Adreno: R11G11B10_FLOAT has no sign bit and cannot store negative values (negatives are clamped to 0 or cause undefined behavior)"
  ]
}
```

> **Design intent**: the `known_gpu_quirks` field automatically attaches known pitfalls based on the GPU model.
> This lets the LLM tie later analysis directly to hardware characteristics.

---

## Module 2: Event and Draw Call Browsing

---

### 2.1 `list_events`

Get the list of events in the frame.

```json
{
  "name": "list_events",
  "description": "Get the list of events in the frame as a tree grouped by render pass. Supports filtering by name (fuzzy match) and by event type. The returned event_id is used in all subsequent queries. Recommended: browse the frame structure quickly with brief=true, then query the passes of interest in detail.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "filter": {
        "type": "string",
        "description": "Fuzzy-filter by name, e.g. 'shadow', 'reflection', 'TAA', 'bloom', 'postprocess'"
      },
      "pass_name": {
        "type": "string",
        "description": "Return only events inside the specified render pass"
      },
      "event_type": {
        "type": "string",
        "enum": ["all", "draw", "dispatch", "clear", "copy", "resolve"],
        "description": "Filter by event type, default all"
      },
      "brief": {
        "type": "boolean",
        "description": "true = return only event_id + name (quick browse); false = include draw parameters and bound shader names. Default true"
      }
    },
    "required": []
  }
}
```

---

### 2.2 `get_event_detail`

Get full information for a single event.

```json
{
  "name": "get_event_detail",
  "description": "Get full details for the specified event: draw call parameters, bound shaders, all bound textures and buffers, render targets, viewport / scissor, and a pipeline state summary. This is the main entry point for investigating a specific draw call. The returned resource_id can be used with resource query tools such as get_texture_info and read_texture_pixels.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "event_id": {
        "type": "integer",
        "description": "Event ID, obtained via list_events"
      }
    },
    "required": ["event_id"]
  }
}
```

**Return value**:
```json
{
  "event_id": 1247,
  "name": "DrawIndexed(3456)",
  "parent_pass": "SceneCapture_Reflection",
  "draw_info": {
    "index_count": 3456,
    "instance_count": 1,
    "vertex_offset": 0
  },
  "shaders": {
    "vertex": { "name": "MobileBasePass.usf", "entry": "Main", "resource_id": 42 },
    "fragment": { "name": "MobileBasePass.usf", "entry": "Main", "resource_id": 43 }
  },
  "bound_textures": [
    { "slot": 0, "name": "SceneColorTexture", "format": "R11G11B10_FLOAT", "size": "1200x540", "resource_id": 101 },
    { "slot": 1, "name": "GBufferA", "format": "R8G8B8A8_UNORM", "size": "1200x540", "resource_id": 102 }
  ],
  "render_targets": [
    { "slot": 0, "name": "ReflectionRT", "format": "R11G11B10_FLOAT", "size": "600x270" }
  ],
  "depth_target": { "name": "SceneDepth", "format": "D24_UNORM_S8_UINT" },
  "pipeline_state_summary": {
    "blend": "Src=One, Dst=Zero (opaque overwrite)",
    "depth_test": "enabled, func=LessEqual, write=true",
    "cull_mode": "Back",
    "stencil": "disabled"
  },
  "next_steps_hint": "For detailed pipeline state use get_pipeline_state; to view shader source use get_shader_source"
}
```

---

## Module 3: Deep Pipeline State Queries

---

### 3.1 `get_pipeline_state`

Get the complete render pipeline state for the specified event.

```json
{
  "name": "get_pipeline_state",
  "description": "Get the complete render pipeline state for the specified event, including all parameters for blend, depth/stencil, rasterizer, viewport/scissor, and multisample. Use when the pipeline_state_summary from get_event_detail is not detailed enough. Commonly used to investigate: dark reflection color (blend configuration / color write mask issues), disappearing objects (depth / cull issues), incorrect transparency, etc.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "event_id": {
        "type": "integer",
        "description": "Event ID"
      },
      "section": {
        "type": "string",
        "enum": ["all", "blend", "depth_stencil", "rasterizer", "viewport", "multisample"],
        "description": "View only a specific section, default all"
      }
    },
    "required": ["event_id"]
  }
}
```

---

### 3.2 `compare_pipeline_state`

Compare the pipeline state of two events and highlight the differences.

```json
{
  "name": "compare_pipeline_state",
  "description": "Compare the render pipeline state of two events and return only the differing fields. Each difference comes with an implication to help locate the problem. Typical use: compare the draw call of an object with that of its reflection to find out why the reflection is darker (possibly a different blend, shader variant, color write mask, or gamma handling). Can also compare how the same object renders in different passes.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "event_id_a": {
        "type": "integer",
        "description": "Reference event (usually the 'correct' one)"
      },
      "event_id_b": {
        "type": "integer",
        "description": "Event to compare (usually the 'problematic' one)"
      }
    },
    "required": ["event_id_a", "event_id_b"]
  }
}
```

**Return value**:
```json
{
  "differences": [
    {
      "field": "fragment_shader",
      "event_a": "MobileBasePass.usf (permutation: REFLECTION=0)",
      "event_b": "MobileBasePass.usf (permutation: REFLECTION=1)",
      "implication": "The reflection pass uses a different shader variant branch, which may contain different lighting / tonemapping logic"
    },
    {
      "field": "blend.color_src_factor",
      "event_a": "One",
      "event_b": "SrcAlpha",
      "implication": "The reflection pass uses alpha blend, which may attenuate the color by the alpha value and make it darker"
    }
  ],
  "identical_sections": ["depth_stencil", "rasterizer", "viewport"],
  "summary": "Found 2 differences, concentrated in shader variant and blend configuration. The blend change from One to SrcAlpha is the most likely cause of the darker reflection."
}
```

---

## Module 4: Shader Analysis

---

### 4.1 `get_shader_source`

Get shader source code or decompiled output.

```json
{
  "name": "get_shader_source",
  "description": "Get the source code of the shader bound at the specified event. Vulkan returns GLSL/HLSL decompiled from SPIR-V; OpenGL ES returns the original GLSL. Used to analyze shader logic errors, precision issues (negative values from half float), IBL calculation errors, tonemapping overflow, etc. If the shader is long, use line_range to view only the key section.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "event_id": {
        "type": "integer",
        "description": "Event ID"
      },
      "stage": {
        "type": "string",
        "enum": ["vertex", "fragment", "compute"],
        "description": "Shader stage, default fragment"
      },
      "format": {
        "type": "string",
        "enum": ["hlsl", "glsl", "spirv_disasm"],
        "description": "Output format preference, default chosen automatically based on API"
      },
      "line_range": {
        "type": "array",
        "items": { "type": "integer" },
        "description": "[start_line, end_line], return only code in the given line range"
      },
      "search": {
        "type": "string",
        "description": "Search the shader source for a keyword and return only matching lines with context (5 lines before and after). E.g. 'ibl', 'tonemap', 'clamp', 'saturate'"
      }
    },
    "required": ["event_id"]
  }
}
```

---

### 4.2 `get_shader_constants`

Get the actual values of the shader constant buffers.

```json
{
  "name": "get_shader_constants",
  "description": "Get the shader constant values for the specified event (current values of Constant Buffers / Uniform Buffers). Essential for investigating: abnormal IBL intensity coefficients, non-orthogonal reflection matrices, exposure overflow, abnormal HDR tonemapping parameters, TAA jitter offset / feedback weights, etc. Can filter by variable name to see only the values of interest. Note: values in the CB are float32, but the GPU may actually compute in half float (lower precision).",
  "inputSchema": {
    "type": "object",
    "properties": {
      "event_id": {
        "type": "integer",
        "description": "Event ID"
      },
      "stage": {
        "type": "string",
        "enum": ["vertex", "fragment", "compute"],
        "description": "Shader stage, default fragment"
      },
      "buffer_slot": {
        "type": "integer",
        "description": "View only the buffer at the given slot; if omitted, return all"
      },
      "filter": {
        "type": "string",
        "description": "Fuzzy-filter by variable name, e.g. 'reflection', 'exposure', 'ibl', 'taa', 'jitter'"
      }
    },
    "required": ["event_id"]
  }
}
```

---

### 4.3 `debug_shader_at_pixel`

Step through the shader at the given pixel position and return intermediate variable values at each step.

```json
{
  "name": "debug_shader_at_pixel",
  "description": "Step-by-step debug the shader at the given pixel position, returning the value of each variable at key steps of execution. This is the most powerful tool for diagnosing precision and calculation errors. Typical uses: (1) IBL yields negative numbers: trace the whole path of SH sampling → multiply by baseColor → tonemapping to find which step produces the negative; (2) TAA bright flashes: trace history sampling → motion vector → color clipping → blend; (3) color shift: trace the full chain of texture sampling → lighting calculation → output. Anomalous values (NaN / Inf / negative) are flagged automatically. Note: this operation is slow (several seconds); use it selectively.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "event_id": {
        "type": "integer",
        "description": "Event ID"
      },
      "pixel_x": {
        "type": "integer",
        "description": "Pixel X coordinate (render target space, origin at top-left)"
      },
      "pixel_y": {
        "type": "integer",
        "description": "Pixel Y coordinate"
      },
      "stage": {
        "type": "string",
        "enum": ["vertex", "fragment"],
        "description": "Debug stage, default fragment"
      },
      "watch_variables": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Trace only these variables, e.g. ['color', 'iblDiffuse', 'exposure']. If omitted, all variables are returned (large output)"
      },
      "max_steps": {
        "type": "integer",
        "description": "Maximum number of trace steps, default 500"
      }
    },
    "required": ["event_id", "pixel_x", "pixel_y"]
  }
}
```

**Return value** (optimized for LLM readability):
```json
{
  "pixel": [1024, 512],
  "shader": "MobileBasePass.usf",
  "execution_trace": [
    {
      "line": 142,
      "source": "half3 iblDiffuse = SampleSH(worldNormal);",
      "outputs": { "iblDiffuse": [0.35, 0.28, -0.02] },
      "warning": "⚠️ iblDiffuse.b = -0.02, SH sampling produces a negative value in this normal direction"
    },
    {
      "line": 145,
      "source": "half3 finalColor = baseColor * iblDiffuse * iblIntensity;",
      "inputs": { "baseColor": [0.8, 0.7, 0.6], "iblDiffuse": [0.35, 0.28, -0.02], "iblIntensity": 1.5 },
      "outputs": { "finalColor": [0.42, 0.294, -0.018] },
      "warning": "⚠️ finalColor.b = -0.018, the negative value is amplified by the multiplication"
    }
  ],
  "final_output": [0.42, 0.294, -0.018, 1.0],
  "anomalies_summary": "2 negative values detected. Root cause: SH sampling at line 142 produces a negative B channel (-0.02), which propagates to finalColor through the multiplication at line 145. Suggest adding a max(0, x) clamp after SH sampling."
}
```

---

## Module 5: Pixel-Level Diagnosis

---

### 5.1 `pixel_history`

Get the full history of modifications to the given pixel over the whole frame.

```json
{
  "name": "pixel_history",
  "description": "Get the full history of all draw calls that modified the given pixel: color values before and after each modification, depth / stencil test results, and blend equations. The most direct way to answer 'why is this pixel this color'. Also lists draw calls that were rejected (depth test fail, etc.) and the values they would have written. Typical uses: dark reflection (compare writes to the same pixel by normal rendering and the reflection pass), disappearing object (check whether it was rejected by depth/stencil), abnormal color (trace all blend accumulation).",
  "inputSchema": {
    "type": "object",
    "properties": {
      "pixel_x": {
        "type": "integer",
        "description": "Pixel X coordinate"
      },
      "pixel_y": {
        "type": "integer",
        "description": "Pixel Y coordinate"
      },
      "target_resource_id": {
        "type": "integer",
        "description": "Which render target to query history on. If omitted, the final backbuffer is used. The resource_id of a specific RT can be obtained via get_event_detail"
      }
    },
    "required": ["pixel_x", "pixel_y"]
  }
}
```

**Return value**:
```json
{
  "pixel": [1024, 512],
  "final_color": [0.65, 0.42, 0.38, 1.0],
  "modifications": [
    {
      "event_id": 245,
      "name": "Clear(SceneColor)",
      "action": "clear",
      "color_after": [0, 0, 0, 1]
    },
    {
      "event_id": 1023,
      "name": "DrawIndexed - SM_Rock_01",
      "action": "draw",
      "shader_output": [0.72, 0.48, 0.41, 1.0],
      "color_after": [0.72, 0.48, 0.41, 1.0],
      "depth_test": "passed (0.847 < 1.0)",
      "blend": "One / Zero (opaque overwrite)",
      "passed": true
    },
    {
      "event_id": 2105,
      "name": "DrawIndexed - TAA",
      "action": "draw",
      "shader_output": [0.65, 0.42, 0.38, 1.0],
      "color_after": [0.65, 0.42, 0.38, 1.0],
      "blend": "One / Zero",
      "passed": true
    }
  ],
  "rejected": [
    {
      "event_id": 1247,
      "name": "DrawIndexed - SM_Rock_01_Reflection",
      "reject_reason": "depth_test_failed (0.923 > 0.847)",
      "would_have_written": [0.52, 0.35, 0.30, 1.0]
    }
  ]
}
```

---

### 5.2 `sample_pixel_region`

Scan region pixel values in bulk and detect anomalies automatically.

```json
{
  "name": "sample_pixel_region",
  "description": "Sample pixel values in bulk on the given render target, automatically detecting NaN / Inf / negative / extremely bright values. This is the first-choice tool for investigating bright-flash bugs: you don't need to know which pixel is bad, just scan the whole screen for anomalous regions. On mobile, bright flashes usually originate from some pixels becoming Inf / NaN or extremely large values that then spread across the whole image through TAA history feedback. This tool quickly locates the 'epicenter'.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "event_id": {
        "type": "integer",
        "description": "Sample on the state after this event. If omitted, the final output is used"
      },
      "resource_id": {
        "type": "integer",
        "description": "resource_id of the render target. If omitted, the output RT of the current event is used"
      },
      "region": {
        "type": "object",
        "description": "Sampling region {x, y, width, height}. If omitted, uniform full-screen sampling is used",
        "properties": {
          "x": { "type": "integer" },
          "y": { "type": "integer" },
          "width": { "type": "integer" },
          "height": { "type": "integer" }
        }
      },
      "sample_count": {
        "type": "integer",
        "description": "Number of sample points, default 256, uniformly distributed"
      },
      "anomaly_threshold": {
        "type": "number",
        "description": "Brightness above this value is flagged as anomalous (for HDR RTs), default 10.0"
      }
    },
    "required": []
  }
}
```

**Return value**:
```json
{
  "render_target": "SceneColor (R11G11B10_FLOAT)",
  "total_samples": 256,
  "anomalies": {
    "nan_count": 0,
    "inf_count": 2,
    "negative_count": 14,
    "extreme_bright_count": 8,
    "anomaly_rate": "9.4%"
  },
  "hotspots": [
    { "pixel": [834, 256], "value": ["+Inf", 342.5, 0.8], "type": "inf" },
    { "pixel": [412, 789], "value": [-0.05, 0.3, 0.28], "type": "negative" }
  ],
  "statistics": {
    "min": [-0.05, -0.02, -0.01],
    "max": ["+Inf", 342.5, 1.2],
    "mean": [2.84, 0.67, 0.52],
    "median": [0.45, 0.38, 0.32]
  },
  "diagnosis_hint": "Inf is concentrated in region (830-840, 250-260), negatives are scattered across the frame. Suggest running pixel_history on anomalous pixels to find the write source, and checking whether the TAA history buffer has accumulated negative values."
}
```

---

## Module 6: Resource Inspection

---

### 6.1 `get_texture_info`

Get texture metadata and value range statistics.

```json
{
  "name": "get_texture_info",
  "description": "Get a texture's format, size, mip count, and numeric statistics of its content (min / max / mean / NaN detection). Does not return full pixel data. Used to quickly judge whether a texture is abnormal: all black? contains NaN? abnormal value range? format differs from expected (e.g. should be SRGB but is actually UNORM, causing double gamma correction)? does some face of the IBL cubemap contain negative values?",
  "inputSchema": {
    "type": "object",
    "properties": {
      "resource_id": {
        "type": "integer",
        "description": "Texture resource_id, obtained from bound_textures of get_event_detail"
      },
      "mip_level": {
        "type": "integer",
        "description": "Mip level to inspect, default 0"
      },
      "all_slices": {
        "type": "boolean",
        "description": "true = compute statistics separately for each array slice / cubemap face. For a cubemap, returns statistics for each of the 6 faces. Default false"
      }
    },
    "required": ["resource_id"]
  }
}
```

**Return value** (cubemap example):
```json
{
  "name": "IBL_Cubemap",
  "format": "R16G16B16A16_FLOAT",
  "size": "256x256",
  "mip_count": 9,
  "array_size": 6,
  "type": "cubemap",
  "per_face_statistics": [
    { "face": "+X", "min": [0.0, 0.0, 0.0], "max": [4.2, 3.8, 5.1], "has_nan": false, "has_negative": false },
    { "face": "-X", "min": [-0.003, 0.0, 0.0], "max": [3.1, 2.9, 4.0], "has_nan": false, "has_negative": true },
    { "face": "+Y", "min": [0.0, 0.0, 0.0], "max": [12.5, 11.8, 15.2], "has_nan": false, "has_negative": false },
    { "face": "-Y", "min": [0.0, 0.0, 0.0], "max": [0.8, 0.7, 0.6], "has_nan": false, "has_negative": false },
    { "face": "+Z", "min": [0.0, 0.0, 0.0], "max": [2.5, 2.1, 3.3], "has_nan": false, "has_negative": false },
    { "face": "-Z", "min": [0.0, 0.0, -0.001], "max": [3.8, 3.5, 4.7], "has_nan": false, "has_negative": true }
  ],
  "warnings": [
    "⚠️ The -X and -Z faces contain tiny negative values, possibly introduced during SH projection or filtering"
  ]
}
```

---

### 6.2 `read_texture_pixels`

Read actual pixel values for a given region of a texture.

```json
{
  "name": "read_texture_pixels",
  "description": "Read actual pixel values (float precision) in the given region of a texture. Region size is limited to 64x64 to control the amount of returned data. Used for precise inspection: specific values of the IBL cubemap in some direction, SH coefficient texture contents, LUT sampled values, accumulated values in the history buffer, etc.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "resource_id": { "type": "integer", "description": "Texture resource_id" },
      "x": { "type": "integer" },
      "y": { "type": "integer" },
      "width": { "type": "integer", "description": "Max 64" },
      "height": { "type": "integer", "description": "Max 64" },
      "mip_level": { "type": "integer", "description": "Mip level, default 0" },
      "array_slice": { "type": "integer", "description": "Array / cubemap slice, cubemap: 0=+X 1=-X 2=+Y 3=-Y 4=+Z 5=-Z" }
    },
    "required": ["resource_id", "x", "y", "width", "height"]
  }
}
```

---

### 6.3 `read_buffer_data`

Read buffer contents.

```json
{
  "name": "read_buffer_data",
  "description": "Read the contents of a buffer (Vertex Buffer / Index Buffer / Constant Buffer / SSBO). Raw data can be interpreted as float32 / float16 / uint etc., or a Vertex Buffer can be parsed automatically into per-vertex attributes using the bound layout. Used to investigate: NaN vertex positions, abnormal matrices in a constant buffer, wrong compute results in an SSBO, etc.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "resource_id": { "type": "integer" },
      "offset_bytes": { "type": "integer", "description": "Start offset, default 0" },
      "length_bytes": { "type": "integer", "description": "Read length, default 1024, max 4096" },
      "interpret_as": {
        "type": "string",
        "enum": ["raw_hex", "float32", "float16", "uint32", "uint16", "vertex_buffer"],
        "description": "How to interpret the data. vertex_buffer parses using the bound layout"
      },
      "event_id": {
        "type": "integer",
        "description": "Associated event ID (used in vertex_buffer mode to get the layout)"
      }
    },
    "required": ["resource_id"]
  }
}
```

---

## Module 7: Performance Analysis

---

### 7.1 `get_pass_timing`

Get GPU time for each render pass / draw call.

```json
{
  "name": "get_pass_timing",
  "description": "Get GPU time statistics for each render pass or draw call in the frame, sorted by time descending by default. Used to quickly locate performance bottlenecks. Note: timing on mobile tile-based GPUs may not fully reflect the actual cost (tile load/store is not timed separately).",
  "inputSchema": {
    "type": "object",
    "properties": {
      "granularity": {
        "type": "string",
        "enum": ["pass", "draw_call"],
        "description": "Statistics granularity, default pass"
      },
      "sort_by": {
        "type": "string",
        "enum": ["time_desc", "time_asc", "order"],
        "description": "Sort order, default time_desc"
      },
      "top_n": {
        "type": "integer",
        "description": "Return only the top N, default 20"
      }
    },
    "required": []
  }
}
```

---

### 7.2 `analyze_overdraw`

Analyze overdraw.

```json
{
  "name": "analyze_overdraw",
  "description": "Analyze overdraw in the frame: average overdraw ratio, the regions with the worst overdraw, and the draw calls causing the most overdraw. Fill rate and bandwidth are the main bottlenecks on mobile; overdraw directly affects frame rate and power consumption. Can analyze only specified passes (such as the opaque or transparent pass).",
  "inputSchema": {
    "type": "object",
    "properties": {
      "pass_name": { "type": "string", "description": "Analyze only the given pass; if omitted, the whole frame is analyzed" },
      "region": {
        "type": "object",
        "description": "Analyze only the given region {x, y, width, height}",
        "properties": {
          "x": { "type": "integer" }, "y": { "type": "integer" },
          "width": { "type": "integer" }, "height": { "type": "integer" }
        }
      }
    },
    "required": []
  }
}
```

---

### 7.3 `analyze_bandwidth`

Estimate memory bandwidth consumption.

```json
{
  "name": "analyze_bandwidth",
  "description": "Estimate memory bandwidth consumed by frame rendering: total and distribution of texture reads, RT reads/writes, and buffer reads. On tile-based GPUs (mobile), also analyzes tile load/store cost and identifies unnecessary full-screen resolves. Bandwidth is the biggest performance bottleneck and heat source for mobile GPUs.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "breakdown_by": {
        "type": "string",
        "enum": ["pass", "resource_type", "operation"],
        "description": "Breakdown dimension, default pass"
      }
    },
    "required": []
  }
}
```

---

### 7.4 `analyze_state_changes`

Analyze state changes between draw calls and batching opportunities.

```json
{
  "name": "analyze_state_changes",
  "description": "Analyze the frequency of render state changes between draw calls in the frame and identify batching opportunities. Counts the number and pattern of shader / texture / RT / blend state changes, and returns which consecutive draw calls have identical state and can be merged, and how many changes would be saved by reordering rendering.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "pass_name": { "type": "string" },
      "change_types": {
        "type": "array",
        "items": {
          "type": "string",
          "enum": ["shader", "texture", "render_target", "blend", "depth_stencil", "vertex_buffer"]
        },
        "description": "Change types of interest, default all"
      }
    },
    "required": []
  }
}
```

---

## Module 8: Smart Diagnostics (High-Level Wrappers)

This layer wraps multi-step low-level operations into a single diagnostic flow aimed at specific bug types.

---

### 8.1 `diagnose_negative_values`

Full-frame scan for negative values, covering scenarios such as IBL negatives and TAA bright flashes.

```json
{
  "name": "diagnose_negative_values",
  "description": "Scan all float-format render targets in the whole frame for pixels with negative values, locate the draw call where negatives first appear, and trace their source. Automatically analyzes whether the TAA history buffer is amplifying negatives (per-frame accumulating spread is the typical bright-flash pattern). Returns root cause candidates and fix suggestions. One-stop coverage of the full chain 'IBL yields negatives' → 'TAA amplifies' → 'bright flashes'.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "check_targets": {
        "type": "array",
        "items": { "type": "string" },
        "description": "RT names to check, e.g. ['SceneColor', 'TAAHistory']. If omitted, all float RTs are checked"
      },
      "trace_depth": {
        "type": "integer",
        "description": "How many events to trace back after finding negatives to locate the source, default 5"
      }
    },
    "required": []
  }
}
```

**Return value**:
```json
{
  "scan_result": "NEGATIVE_VALUES_DETECTED",
  "affected_targets": [
    {
      "name": "SceneColor",
      "format": "R11G11B10_FLOAT",
      "negative_pixel_count": 47,
      "negative_rate": "0.8%",
      "first_introduced_at": {
        "event_id": 892,
        "name": "DrawIndexed - IBL_Apply",
        "new_negative_pixels": 23,
        "sample": { "pixel": [412, 789], "value": [-0.05, 0.3, 0.28] }
      }
    },
    {
      "name": "TAAHistory",
      "format": "R16G16B16A16_FLOAT",
      "negative_pixel_count": 183,
      "negative_rate": "3.2%",
      "first_introduced_at": {
        "event_id": 2105,
        "name": "DrawIndexed - TAA_Resolve",
        "new_negative_pixels": 183
      },
      "accumulation_warning": "TAAHistory negative count (183) is 3.9 times SceneColor's (47); TAA feedback is amplifying negatives, and continued accumulation will cause bright flashes"
    }
  ],
  "root_cause_candidates": [
    {
      "likelihood": "high",
      "cause": "IBL SH sampling produces negative values in some normal directions",
      "evidence": "event 892 first introduces 23 negative pixels",
      "fix": "Add a max(0, result) clamp after SH sampling, or use a non-negative SH basis"
    },
    {
      "likelihood": "high",
      "cause": "TAA color clipping does not handle negative input",
      "evidence": "TAAHistory has 3.9 times as many negatives as SceneColor",
      "fix": "In the TAA shader, clamp the lower bound of the history sample and neighborhood AABB to 0"
    }
  ]
}
```

---

### 8.2 `diagnose_precision_issues`

Detect half float precision problems, covering mobile precision bugs.

```json
{
  "name": "diagnose_precision_issues",
  "description": "Detect possible floating-point precision problems in the frame. Analyzes: (1) precision loss on half float format RTs (compared with a float32 reference, if available); (2) behavior of mediump variables in shaders near extreme values; (3) special limitations of R11G11B10_FLOAT (no sign bit, limited precision); (4) insufficient depth buffer precision causing z-fighting. Returns a list of detected precision issues with an impact assessment for each.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "focus": {
        "type": "string",
        "enum": ["all", "color_precision", "depth_precision", "shader_precision"],
        "description": "Precision dimension of interest, default all"
      },
      "threshold": {
        "type": "number",
        "description": "Report only when the precision deviation exceeds this threshold (relative error), default 0.01 (1%)"
      }
    },
    "required": []
  }
}
```

**Return value**:
```json
{
  "issues_found": [
    {
      "type": "format_limitation",
      "target": "SceneColor (R11G11B10_FLOAT)",
      "description": "R11G11B10_FLOAT has no sign bit and cannot store negative values. Any negative value written to this RT is clamped to 0 or causes undefined behavior.",
      "affected_events": [892, 945, 1023],
      "severity": "high",
      "fix": "If legitimate negative values exist in the pipeline (such as from SH sampling), clamp to [0, max] before writing to this RT, or switch to R16G16B16A16_FLOAT (at the cost of more bandwidth)"
    },
    {
      "type": "depth_precision",
      "description": "The D24 depth buffer lacks precision near the far plane; with near=0.1 and far=1000, precision beyond z=500 is about 0.3 world units",
      "z_fighting_risk_zones": [
        { "depth_range": [800, 1000], "precision": "~1.2 world units", "risk": "high" }
      ],
      "severity": "medium",
      "fix": "Consider reversed-Z + D32_FLOAT, or shrink the far plane"
    }
  ]
}
```

---

### 8.3 `diagnose_reflection_mismatch`

Dedicated diagnosis of reflection color mismatch.

```json
{
  "name": "diagnose_reflection_mismatch",
  "description": "Diagnose color differences between a reflection and its original object (e.g. dark reflection, color cast, missing highlights). Automatically: (1) identifies reflection-related passes in the frame (SceneCapture / Planar Reflection / SSR, etc.); (2) finds the draw calls for the same object in normal rendering and in the reflection pass; (3) compares their shader variants, pipeline state, bound textures, and constants; (4) compares final color values at pixels that can be paired. Returns all differences and their impact analysis on color.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "reflection_pass_hint": {
        "type": "string",
        "description": "Name hint for the reflection pass, e.g. 'SceneCapture', 'PlanarReflection', 'SSR'. Detected automatically if omitted"
      },
      "object_hint": {
        "type": "string",
        "description": "Name hint for the object to compare, e.g. 'SM_Rock'. If omitted, the object with the largest difference is chosen"
      }
    },
    "required": []
  }
}
```

**Return value**:
```json
{
  "reflection_type": "SceneCapture (Planar Reflection)",
  "paired_objects": [
    {
      "object": "SM_Building_01",
      "normal_event_id": 1023,
      "reflection_event_id": 1247,
      "color_difference": {
        "sample_pixel_normal": [0.72, 0.48, 0.41],
        "sample_pixel_reflection": [0.52, 0.35, 0.30],
        "brightness_ratio": 0.72,
        "description": "The reflection is about 28% darker than normal rendering"
      },
      "causes": [
        {
          "factor": "shader_variant",
          "detail": "The reflection pass uses the REFLECTION=1 variant, which skips the IBL specular calculation",
          "estimated_impact": "Brightness reduced by ~15%",
          "fix": "Keep the IBL specular term in the REFLECTION=1 variant, or use a simplified version"
        },
        {
          "factor": "blend_state",
          "detail": "The reflection pass's blend src changes from One to SrcAlpha, and the alpha value is 0.85",
          "estimated_impact": "Brightness reduced by ~15%",
          "fix": "Check whether the reflection pass needs alpha blend; if not, change it to One/Zero"
        },
        {
          "factor": "render_target_format",
          "detail": "The reflection RT resolution (600x270) is 1/4 of the main RT (1200x540), but the format is the same",
          "estimated_impact": "Resolution does not affect brightness, but may affect highlight detail",
          "fix": "No change needed"
        }
      ],
      "total_estimated_brightness_loss": "~28% (matches the measured 28%)"
    }
  ]
}
```

> **Design intent**: this tool fully covers the investigation of "why is the reflection darker".
> The LLM does not need to find the reflection pass, pair draw calls, and compare state itself.
> It is done in one step.

---

### 8.4 `diagnose_mobile_risks`

Pre-check common mobile risks based on frame data.

```json
{
  "name": "diagnose_mobile_risks",
  "description": "Based on the current frame data, check thoroughly for common mobile rendering risks and potential bugs. Checks include: (1) precision risks: extreme values in half float RTs, negatives stored in R11G11B10, insufficient depth precision; (2) performance risks: excessive overdraw, unnecessary full-screen passes, RTs with excessive bandwidth, too many state changes; (3) compatibility risks: reliance on desktop features (such as geometry shaders), texture sizes or sampler counts exceeding mobile limits; (4) known GPU-specific issues (based on the GPU model from get_capture_info). Returns a risk list with priority ordering.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "check_categories": {
        "type": "array",
        "items": {
          "type": "string",
          "enum": ["precision", "performance", "compatibility", "gpu_specific"]
        },
        "description": "Categories to check, default all"
      },
      "severity_filter": {
        "type": "string",
        "enum": ["all", "high", "medium"],
        "description": "Return only risks at or above the given severity, default all"
      }
    },
    "required": []
  }
}
```

**Return value**:
```json
{
  "device": "Adreno 640 / Vulkan 1.1",
  "risks": [
    {
      "category": "precision",
      "severity": "high",
      "title": "R11G11B10_FLOAT negative value storage risk",
      "detail": "SceneColor uses R11G11B10_FLOAT (no sign bit), but the IBL pass writes negative values. On Adreno negatives are clamped to 0; on Mali the behavior is undefined.",
      "affected_events": [892, 945],
      "fix": "Clamp IBL shader output to 0, or change SceneColor to R16G16B16A16_FLOAT"
    },
    {
      "category": "precision",
      "severity": "high",
      "title": "TAA negative accumulation risks bright flashes",
      "detail": "183 negative pixels detected in TAAHistory (R16G16B16A16_FLOAT), and the count exceeds that of the upstream SceneColor, indicating TAA feedback is accumulating and amplifying negatives.",
      "fix": "In the TAA shader, clamp the history sample and AABB lower bound to 0"
    },
    {
      "category": "performance",
      "severity": "high",
      "title": "Too many full-screen passes",
      "detail": "8 full-screen post-process passes detected, totaling 4.2ms (25% of frame time). Of these, Bloom has 4 downsample + 4 upsample levels, 8 passes in total.",
      "fix": "Consider merging bloom passes (merge each level's downsample and upsample into a single pass), or reduce the number of bloom levels"
    },
    {
      "category": "performance",
      "severity": "medium",
      "title": "Transparent overdraw too high",
      "detail": "The transparent pass averages 3.7x overdraw, up to 8.2x in the screen center (where particle effects overlap).",
      "fix": "Reduce particle layers, use smaller particle quads, enable soft particle culling"
    },
    {
      "category": "performance",
      "severity": "medium",
      "title": "Unnecessary RT resolve",
      "detail": "3 MSAA resolve operations detected, but later passes do not use the resolved data. Each resolve has a significant bandwidth cost on tile-based GPUs.",
      "fix": "Remove unused resolves, or use subpass automatic resolve"
    },
    {
      "category": "compatibility",
      "severity": "low",
      "title": "Sampler count near the limit",
      "detail": "The fragment shader of event 1023 binds 14 texture samplers; some low-end mobile GPUs limit this to 16.",
      "fix": "Merge into texture atlases or use texture arrays to reduce the number of bound samplers"
    },
    {
      "category": "gpu_specific",
      "severity": "medium",
      "title": "Adreno mediump precision below standard",
      "detail": "Adreno 640's mediump float precision is about the minimum FP16 requirement, which may produce visible artifacts in normal reconstruction and lighting calculations.",
      "fix": "Force highp for critical calculations (normal reconstruction, reflection direction calculation)"
    }
  ],
  "summary": "Found 7 risk items (2 high / 3 medium / 1 low / 1 gpu_specific). Top priority: fix the IBL negative values and TAA negative accumulation to remove the bright-flash risk."
}
```

---

## Tool Overview

| # | Tool | Module | Core Purpose |
|---|--------|------|---------|
| 1 | `load_capture` | Capture File Management | Load .rdc file |
| 2 | `get_capture_info` | Capture File Management | Get device / environment info |
| 3 | `list_events` | Event Browsing | Browse all events in the frame (with filtering) |
| 4 | `get_event_detail` | Event Browsing | Get full information for a single event |
| 5 | `get_pipeline_state` | Pipeline State | Get full render pipeline state |
| 6 | `compare_pipeline_state` | Pipeline State | Compare state differences between two events |
| 7 | `get_shader_source` | Shader Analysis | Get shader source (with search) |
| 8 | `get_shader_constants` | Shader Analysis | Get constant buffer values |
| 9 | `debug_shader_at_pixel` | Shader Analysis | Step-by-step shader debugging at a single pixel |
| 10 | `pixel_history` | Pixel Diagnosis | Full modification history of a pixel |
| 11 | `sample_pixel_region` | Pixel Diagnosis | Bulk region sampling + anomaly detection |
| 12 | `get_texture_info` | Resource Inspection | Texture metadata + value range statistics |
| 13 | `read_texture_pixels` | Resource Inspection | Read texture region pixel values |
| 14 | `read_buffer_data` | Resource Inspection | Read buffer contents |
| 15 | `get_pass_timing` | Performance Analysis | GPU time per pass |
| 16 | `analyze_overdraw` | Performance Analysis | Overdraw analysis |
| 17 | `analyze_bandwidth` | Performance Analysis | Memory bandwidth estimate |
| 18 | `analyze_state_changes` | Performance Analysis | State change analysis + batching suggestions |
| 19 | `diagnose_negative_values` | Smart Diagnostics | Negative value scan + root cause tracing |
| 20 | `diagnose_precision_issues` | Smart Diagnostics | Precision issue detection |
| 21 | `diagnose_reflection_mismatch` | Smart Diagnostics | Reflection color difference diagnosis |
| 22 | `diagnose_mobile_risks` | Smart Diagnostics | Full mobile risk pre-check |

---

## Tool Call Chain Examples

The following shows how Claude Code chains these tools in real scenarios.

### Scenario A: Reflection darker than the original object

```
User: "The reflection is darker than the original object, help me find the cause"

Claude Code execution chain:
1. load_capture("frame.rdc")
2. diagnose_reflection_mismatch()                    ← done in one step
   → Automatically identifies the reflection pass, pairs draw calls, compares differences
   → Returns: shader variant differences + blend state differences + quantified brightness loss analysis

If deeper investigation is needed (automatic diagnosis is not enough):
3. compare_pipeline_state(event_a=normal DC, event_b=reflection DC)
4. get_shader_source(event_id=reflection DC, search="ibl")
5. debug_shader_at_pixel(event_id=reflection DC, pixel_x=..., pixel_y=...)
```

### Scenario B: IBL yields negative numbers

```
User: "IBL lighting yields negative numbers, help me locate the problem"

Claude Code execution chain:
1. load_capture("frame.rdc")
2. diagnose_negative_values()                        ← full-frame negative scan
   → Locates event 892 (IBL_Apply) as where negatives first appear
3. get_shader_source(event_id=892, search="SampleSH")
   → Checks whether the SH sampling code has a clamp
4. get_shader_constants(event_id=892, filter="sh")
   → Checks whether the SH coefficient values are reasonable
5. debug_shader_at_pixel(event_id=892, pixel_x=412, pixel_y=789, watch=["iblDiffuse"])
   → Traces the specific step that produces the negative
6. get_texture_info(resource_id=IBL_cubemap_id, all_slices=true)
   → Checks whether the cubemap itself contains negatives
```

### Scenario C: Mobile bright flashes (TAA negatives)

```
User: "Bright flashes occasionally appear on the phone; I suspect TAA"

Claude Code execution chain:
1. load_capture("frame_flash.rdc")
2. get_capture_info()                                ← confirm GPU model and known issues
3. diagnose_negative_values(check_targets=["SceneColor","TAAHistory"])
   → Finds negatives accumulating and amplifying in the TAA history
4. sample_pixel_region(event_id=TAA_event, anomaly_threshold=10.0)
   → Locates the exact positions of the bright-flash pixels
5. pixel_history(pixel_x=bright-flash point X, pixel_y=bright-flash point Y)
   → Traces which passes modified this pixel
6. debug_shader_at_pixel(event_id=TAA_event, pixel_x=..., pixel_y=..., watch=["historyColor","clampedColor","outputColor"])
   → Traces the history sampling and color clipping in the TAA shader
7. diagnose_precision_issues(focus="color_precision")
   → Checks whether the R11G11B10 format limitation worsens the problem
```

### Scenario D: Full performance analysis and optimization advice

```
User: "The frame rate misses the target; analyze the performance bottlenecks and suggest optimizations"

Claude Code execution chain:
1. load_capture("frame.rdc")
2. get_capture_info()                                ← confirm device environment
3. get_pass_timing(granularity="pass", top_n=10)     ← find the most expensive passes
4. analyze_overdraw()                                ← check fill rate pressure
5. analyze_bandwidth(breakdown_by="pass")            ← check bandwidth bottlenecks
6. analyze_state_changes()                           ← check batching opportunities
7. diagnose_mobile_risks(check_categories=["performance"])
   → Combines the data above into a prioritized list of optimization suggestions
```

### Scenario E: Mobile bug risk pre-check

```
User: "This frame is fine on PC; predict what bugs might show up on phones"

Claude Code execution chain:
1. load_capture("frame.rdc")
2. get_capture_info()                                ← confirm GPU and known issues
3. diagnose_mobile_risks()                           ← full risk pre-check
   → Returns all precision / performance / compatibility / GPU-specific risks in one call
4. Investigate further based on the items with severity=high
```

---

## Implementation Priority Recommendations

### P0: Core Features (implement first; get the basic workflow running)

| Tool | Rationale |
|------|------|
| `load_capture` | The starting point for everything |
| `get_capture_info` | Device info is essential for mobile diagnosis |
| `list_events` | Need to be able to browse the frame structure |
| `get_event_detail` | Need to be able to inspect specific draw calls |
| `get_pipeline_state` | Many bugs relate to state configuration |
| `get_shader_source` | Need to be able to view shader code |
| `get_shader_constants` | Need to be able to view uniform values |
| `pixel_history` | Cornerstone of pixel-level diagnosis |
| `get_texture_info` | Basis of resource inspection |

### P1: High-Value Features (significantly improve diagnosis efficiency)

| Tool | Rationale |
|------|------|
| `debug_shader_at_pixel` | The most powerful tool for precision and calculation bugs, but high implementation complexity |
| `compare_pipeline_state` | Efficiency tool for comparison problems (dark reflection, etc.) |
| `sample_pixel_region` | First choice for locating bright flashes |
| `read_texture_pixels` | Precise inspection of texture content |
| `diagnose_negative_values` | Wraps the most common bug type |
| `get_pass_timing` | Entry point for performance analysis |

### P2: Enhancements (nice to have)

| Tool | Rationale |
|------|------|
| `read_buffer_data` | Rarely used but occasionally needed |
| `analyze_overdraw` | Supporting data for performance optimization |
| `analyze_bandwidth` | In-depth mobile performance analysis |
| `analyze_state_changes` | Aid for batching optimization |
| `diagnose_precision_issues` | Systematic precision investigation |
| `diagnose_reflection_mismatch` | One-click diagnosis of reflection problems |
| `diagnose_mobile_risks` | Full pre-check |

---

## Implementation Technical Notes

### RenderDoc Python API Mapping

```
MCP tool                      → RenderDoc Python API
──────────────────────────────────────────────────────
load_capture                  → rd.OpenCaptureFile() + ReplayController
get_capture_info              → controller.GetAPIProperties() + GetDriverInfo()
list_events                   → controller.GetDrawcalls() (recursively traverse the tree)
get_event_detail              → controller.SetFrameEvent() + GetD3D11/VulkanPipelineState()
get_pipeline_state            → controller.GetVulkanPipelineState() (or the corresponding API)
get_shader_source             → controller.GetShader() + DisassembleShader()
get_shader_constants          → controller.GetCBuffers() + GetBufferData()
debug_shader_at_pixel         → controller.DebugPixel()
pixel_history                 → controller.PixelHistory()
sample_pixel_region           → controller.GetTextureData() + numpy analysis
get_texture_info              → controller.GetTextures() + GetTextureData() statistics
read_texture_pixels           → controller.GetTextureData() slicing
read_buffer_data              → controller.GetBufferData()
get_pass_timing               → controller.FetchCounters() (if GPU timing is supported)
```

### Notes

1. **The RenderDoc Python module must be built and installed**: the renderdoc Python binding is not a pip package.
   Build it from source, or import `renderdoc.pyd` / `renderdoc.so` from the RenderDoc install directory.
2. **ReplayController is stateful**: `SetFrameEvent(eid)` changes the currently active event.
   All later state queries are based on that event.
   The MCP Server must manage this state.
3. **GPU timing may be unavailable on mobile**: RenderDoc captures from some mobile GPUs do not support timing counters.
   `get_pass_timing` needs graceful degradation.
4. **Memory management for large texture reads**: `GetTextureData()` returns raw bytes.
   Large textures can use a lot of memory, so release them promptly.
5. **Shader debugging platform limits**: `DebugPixel()` may be unsupported on some API / GPU combinations.
   Check `controller.GetDebugMessages()`.
