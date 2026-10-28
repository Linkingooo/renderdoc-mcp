# RenderDoc MCP Optimization Roadmap

> An improvement plan based on the pain points from a full RDC reverse-engineering session on the crystal tail material from Honor of Kings (王者荣耀).
>
> **v0.2.0 update**: 11 of the 13 pain points are done (issues 2-7, 9-13).
> 8 tools were added (23→31).
> See the ✅ marks below.
>
> **v0.3.0 update**: Gaps were filled by comparing against the tool design doc.
> 11 tools were added (31→42).
> 2 modules were added (performance_tools, diagnostic_tools).
> Several existing tools were enhanced.
> See the v0.3 marks below.

---

## 1. Session Retrospective: Problem Summary

### Scenario

Reverse-engineering the rendering technique of the crystal tail from an RDC frame capture of an OpenGL ES mobile game:
- Locate the target draw calls (EID 2480/2530 among 266 draws)
- Analyze blend modes, texture bindings, and shader structure
- Extract texture assets
- Infer the shader logic
- Produce a UE5 recreation plan

### Result

The MCP was not used at all.
The whole session was hand-written Python scripts.
It hit many API errors and was very inefficient.

---

## 2. Problem Categories

### P0: Fundamental Problems (MCP completely unusable)

#### Issue 1: MCP is configured only at project level and is unusable from other directories

**Current state**: `.mcp.json` exists only in `F:/RenderDoc/`.
The MCP is unusable when working under `C:\Users\18523`.

**Improvements**:
- [ ] Recommend user-level configuration in the README
- [ ] Provide a one-step install script that writes `~/.claude/settings.json`

---

### P1: High-Frequency Pain Points (hit in every analysis)

#### Issue 2: No high-level "analyze a draw call in one step" tool

**Current state**: Analyzing one draw call requires:
1. `set_event(eid)`
2. `get_pipeline_state()`: get blend/depth/cull
3. `get_shader_bindings("fragment")`: get texture bindings
4. `get_shader_reflection("fragment")`: get texture names
5. Manually map texture ID → texture size/format

The AI needs 4-5 tool calls to answer "which textures does this draw call use".

**Improvements**:
- [x] **New tool: `get_draw_call_state(event_id)`**: returns the complete state in one call. ✅ `pipeline_tools.py`

```json
{
  "event_id": 2530,
  "action_name": "glDrawElements()",
  "vertex_count": 8808,
  "instance_count": 1,
  "blend": {
    "enabled": true,
    "color_src": "SrcAlpha",
    "color_dst": "SrcColor",
    "alpha_src": "SrcAlpha",
    "alpha_dst": "SrcColor",
    "equation": "result = src.a * src.rgb + src.rgb * dst.rgb"
  },
  "depth": {"test": true, "write": false, "func": "LessEqual"},
  "cull": "Back",
  "two_sided": false,
  "textures": [
    {"slot": 0, "name": "_BumpMap", "id": "ResourceId::28", "size": "4x4", "format": "R8G8B8A8_UNORM"},
    {"slot": 1, "name": "_DiamondMap", "id": "ResourceId::3139", "size": "512x512", "format": "ASTC_UNORM"}
  ],
  "shader": {
    "vertex_id": "ResourceId::3406",
    "fragment_id": "ResourceId::3407",
    "vertex_inputs": ["POSITION", "NORMAL", "TANGENT", "TEXCOORD0", "TEXCOORD1"]
  }
}
```

- [x] **New tool: `get_frame_overview()`**: frame overview. ✅ `session_tools.py`

```json
{
  "api": "OpenGL ES",
  "gpu": "...",
  "resolution": "1496x720",
  "total_actions": 285,
  "draw_calls": 266,
  "dispatches": 0,
  "clears": 18,
  "copies": 0,
  "textures": 172,
  "buffers": 141,
  "render_passes": [
    {"name": "Shadow", "draws": 23, "rt": "187x90 D16"},
    {"name": "Main Scene", "draws": 150, "rt": "1496x720 R8G8B8A8 + D24S8"},
    {"name": "Post Process", "draws": 9, "rt": "1496x720"},
    {"name": "UI", "draws": 60, "rt": "1496x720"}
  ]
}
```

---

#### Issue 3: Blend modes shown as raw numbers

**Current state**: `get_pipeline_state()` returns blend factors as enum integers (`6`, `7`, `2`).
Neither the AI nor a human can read them directly.

**Improvements**:
- [x] Add a BlendMultiplier enum mapping table in `pipeline_tools.py` ✅ `util.py` BLEND_FACTOR_MAP + enum_str()
- [x] Output readable strings: `"SrcAlpha"`, `"InvSrcAlpha"`, `"SrcColor"`, etc. ✅
- [x] Also generate blend formula text: `blend_formula()` ✅
  - Also applied to depth func, cull mode, fill mode, and topology.

Mapping table:
```python
BLEND_FACTOR_MAP = {
    0: "Zero",
    1: "One",
    2: "SrcColor",
    3: "InvSrcColor",
    4: "DstColor",
    5: "InvDstColor",
    6: "SrcAlpha",
    7: "InvSrcAlpha",
    8: "DstAlpha",
    9: "InvDstAlpha",
    10: "SrcAlphaSat",      # GL_SRC_ALPHA_SATURATE
    11: "BlendFactor",
    12: "InvBlendFactor",
    13: "Src1Color",
    14: "InvSrc1Color",
    15: "Src1Alpha",
    16: "InvSrc1Alpha",
}
```

---

#### Issue 4: No draw call comparison tool

**Current state**: The crystal tail uses two passes (EID 2480 opaque + EID 2530 alpha).
The state differences between the two draws must be compared by hand.
With 10 alpha passes it is a nightmare.

**Improvements**:
- [x] **New tool: `diff_draw_calls(eid1, eid2)`**: automatically compares the state of two draw calls. ✅ `advanced_tools.py`

```json
{
  "eid1": 2480,
  "eid2": 2530,
  "diff": {
    "blend": {"eid1": "opaque", "eid2": "SrcAlpha + SrcColor"},
    "depth_write": {"eid1": true, "eid2": false},
    "cull": {"eid1": "Back", "eid2": "Back"},
    "fragment_shader": {"eid1": "ResourceId::3404", "eid2": "ResourceId::3407"},
    "textures": {
      "added": [
        {"slot": 1, "name": "_DiamondMap"},
        {"slot": 4, "name": "_FlowMap"}
      ],
      "removed": [],
      "changed": [
        {"slot": 0, "name": "_DissolveMap -> _BumpMap"}
      ]
    },
    "vertex_count": "same (8808)"
  }
}
```

---

#### Issue 5: Batch texture export is inconvenient

**Current state**: Exporting all textures of a draw call requires `get_shader_bindings` to get the resource IDs.
Then each texture needs its own `save_texture` call.
For a draw call with 10 textures, the AI needs 11 tool calls.

**Improvements**:
- [x] **New tool: `export_draw_textures(event_id, output_dir)`**: exports all bound textures in one call. ✅ `data_tools.py`

```json
{
  "event_id": 2530,
  "output_dir": "C:/output/eid2530_textures/",
  "exported": [
    {"name": "_DiamondMap", "file": "DiamondMap_512x512.png", "size": "512x512"},
    {"name": "_MainTex", "file": "MainTex_1024x1024.png", "size": "1024x1024"}
  ]
}
```

- [x] Automatic file naming: `{uniform_name}_{width}x{height}.png` ✅
- [x] Skip placeholder textures (4x4 or smaller, `skip_small` parameter) ✅

---

#### Issue 6: Render target snapshots are inconvenient

**Current state**: To see what a draw call drew, you must save the RT with `save_texture` and open it in a file viewer.

**Improvements**:
- [x] **New tool: `save_render_target(event_id, output_path)`**: saves the RT state after the current draw call executes. ✅ `data_tools.py`
- [x] Automatically identify the bound color RT + depth RT ✅
- [x] Optional parameter: `save_depth=true` also saves the depth image ✅

---

### P2: Medium-Frequency Pain Points (needed in specific analysis scenarios)

#### Issue 7: Shader source/disassembly is unreliable

**Current state**:
- `disassemble_shader` fails to disassemble GLES shaders via SPIR-V
- No fallback to obtain the original GLSL source
- On failure it returns only an error message, with no alternative

**Improvements**:
- [x] Try every available disassembly target automatically and return the first success ✅ `shader_tools.py`
- [ ] For GL/GLES captures, extract the original GLSL source from the structured file (not yet implemented)
- [x] If all disassembly fails, fall back to returning shader reflection ✅
  - Reflection covers inputs/outputs/uniforms/resources.
- [x] Indicate in the result: `"source_type": "disasm" | "reflection_only"` ✅

---

#### Issue 8: Uniform value reads fail

**Current state**: `get_cbuffer_contents` did not work at all in this session (API parameter mismatch).
We could not read the actual uniform values to compare passes.

**Existing code** (`shader_tools.py`):
```python
# The current implementation may be incompatible with newer RenderDoc APIs
vars = controller.GetCBufferVariableContents(
    pipe.GetGraphicsPipelineObject(),
    pipe.GetShader(stage_enum),
    pipe.GetShaderEntryPoint(stage_enum),
    cbuffer_index,
    cbuf.descriptor.resource,
    cbuf.descriptor.byteOffset,
    cbuf.descriptor.byteSize,
    0
)
```

**Improvements**:
- [ ] Support the parameter signatures of multiple RenderDoc API versions (try/except fallback)
- [ ] For GL/GLES, try reading directly from the GL uniform location
- [ ] Add a high-level `get_uniform_values(event_id, stage)` tool that returns a `{"name": value}` dict
- [ ] Value formatting: show float4 as `[1.0, 0.5, 0.3, 1.0]`.
  Show matrix as `[mat4x4]`.

---

#### Issue 9: No way to search draw calls by condition

**Current state**: `search_actions` only searches by name and flag.
The most common needs in practice are:
- "Find all draw calls that use alpha blend"
- "Find draw calls with more than 5000 vertices"
- "Find draw calls that bind a given texture"

**Improvements**:
- [x] **New tool: `find_draws(filter)`**: searches by render state. ✅ `event_tools.py`

```python
find_draws(
    blend="not_opaque",           # all non-opaque
    min_vertices=5000,            # vertex count filter
    texture_name="_DiamondMap",   # uses a specific texture
    shader_id="ResourceId::3407"  # uses a specific shader
)
```

Note: This tool must iterate over all draw calls and check the state of each.
It can be slow.
Warn the user in advance.

---

#### Issue 10: No automatic render pass segmentation

**Current state**: Render pass boundaries must be found by hand from Clear/Invalidate calls.

**Improvements**:
- [x] **New tool: `analyze_render_passes()`**: detects render pass boundaries automatically and summarizes them. ✅ `advanced_tools.py`

```json
{
  "passes": [
    {
      "index": 0,
      "start_eid": 19,
      "end_eid": 506,
      "draw_count": 23,
      "render_target": {"color": "187x90 R8G8B8A8", "depth": "187x90 D16"},
      "clear_color": [0, 0, 0, 1],
      "description": "Shadow Map"
    }
  ]
}
```

Detection logic:
- Use calls such as `glClear` / `glInvalidateFramebuffer` / `SetRenderTarget` as boundaries
- Detect RT switches (framebuffer changes)
- Summarize the draw count and RT info of each segment

---

### P3: Low-Frequency but Worth Doing

#### Issue 11: pixel_history `passed` field logic is inverted (BUG)

**Existing code** (`advanced_tools.py` line 63):
```python
"passed": not mod.Passed(),  # BUG: logic inverted
```

**Fix**:
- [x] Change to `"passed": mod.Passed()` ✅ Fixed

---

#### Issue 12: Resource ID lookup is inefficient

**Current state**: `_parse_resource_id()` iterates over all textures + buffers + resources on every call.

**Improvements**:
- [x] Build a resource ID → descriptor dict cache in `session.open()` ✅ `_resource_id_cache` + `_texture_desc_cache`
- [x] Later lookups are O(1) ✅ `resolve_resource_id()` + `get_texture_desc()`
  - All modules are migrated.

---

#### Issue 13: No mesh export

**Current state**: The mesh of a draw call cannot be exported to OBJ/FBX.

**Improvements**:
- [x] **New tool: `export_mesh(event_id, output_path)`** ✅ `data_tools.py`
- [x] Use `get_post_vs_data` to get vertex data and convert it to OBJ ✅
- [x] Supports position, normal, uv ✅
  - Semantics are detected automatically from the output signature.

---

## 3. Priority Order

### Batch 1 (is it usable at all)

| # | Improvement | Effort | Impact | Status |
|---|------|--------|------|------|
| 1 | Add user-level configuration docs + install script | Small | Fixes the root cause of the MCP being unusable | To do |
| 3 | Blend mode enum mapping | Small | Large readability gain | ✅ Done |
| 11 | Fix the pixel_history passed field bug | Tiny | Bug fix | ✅ Done |

### Batch 2 (is it pleasant to use)

| # | Improvement | Effort | Impact | Status |
|---|------|--------|------|------|
| 2 | `get_draw_call_state()` + `get_frame_overview()` | Medium | Core experience improvement | ✅ Done |
| 4 | `diff_draw_calls()` | Medium | Comparison analysis efficiency | ✅ Done |
| 5 | `export_draw_textures()` | Small | Texture extraction efficiency | ✅ Done |
| 6 | `save_render_target()` | Small | Quick preview | ✅ Done |

### Batch 3 (is it powerful)

| # | Improvement | Effort | Impact | Status |
|---|------|--------|------|------|
| 7 | Shader disassembly fallback chain | Medium | Shader analysis | ✅ Done (GLSL extraction to do) |
| 8 | Uniform value read fix | Medium | Shader parameter comparison | To do |
| 9 | `find_draws()` state search | Large | Advanced search | ✅ Done |
| 10 | `analyze_render_passes()` | Medium | Automated analysis | ✅ Done |
| 12 | Resource ID cache | Small | Performance optimization | ✅ Done |
| 13 | Mesh export | Medium | Asset extraction | ✅ Done |

---

## 3-B. v0.3.0 Additions and Enhancements

### Tool Enhancements (new parameters on existing tools)

| Tool | New capability | Version |
|------|----------|------|
| `get_capture_info` | Returns `api`, `resolution`, `main_color_format`, `known_gpu_quirks` (auto-detects Adreno/Mali/PowerVR/Apple pitfalls) | **v0.3** |
| `list_actions` | Adds `filter` (name substring filter) and `event_type` (draw/dispatch/clear/copy/resolve) | **v0.3** |
| `disassemble_shader` | Adds `search` (keyword + ±5 lines of context) and `line_range` (line range slice) | **v0.3** |
| `get_cbuffer_contents` | Adds `filter` (variable name substring filter, supports nested structs) | **v0.3** |
| `get_texture_stats` | Adds `all_slices` (iterate all mip/cubemap faces) and anomaly detection (NaN/Inf/negative warnings) | **v0.3** |
| `diff_draw_calls` | Returns a flat `differences` list, each with an `implication` (effect on rendering) | **v0.3** |

### New Tools

| Tool | Module | Description | Version |
|------|------|------|------|
| `read_texture_pixels` | data_tools | Reads a rectangular region of pixels (up to 64×64) with per-pixel anomaly flags | **v0.3** |
| `sample_pixel_region` | advanced_tools | Samples an RT region on a uniform grid, detecting NaN/Inf/negative/overexposed hotspots | **v0.3** |
| `debug_shader_at_pixel` | advanced_tools | Per-pixel shader debugging (DebugPixel); when unsupported, falls back to returning the pixel value + reflection info | **v0.3** |
| `get_pass_timing` | performance_tools | Most expensive render passes, using GPU counters or a triangle-count heuristic | **v0.3** |
| `analyze_overdraw` | performance_tools | Estimates overdraw grouped by RT | **v0.3** |
| `analyze_bandwidth` | performance_tools | Estimates write/read bandwidth per RT | **v0.3** |
| `analyze_state_changes` | performance_tools | Finds redundant state changes and draw call batching opportunities | **v0.3** |
| `diagnose_negative_values` | diagnostic_tools | Scans all float RTs for negative values/NaN/Inf, finds the first event that introduces them, and detects TAA accumulation | **v0.3** |
| `diagnose_precision_issues` | diagnostic_tools | Checks for missing R11G11B10 sign bit, low-precision depth buffers, and SRGB/linear space mismatches | **v0.3** |
| `diagnose_reflection_mismatch` | diagnostic_tools | Compares the reflection pass against main scene draws and reports the shader/blend/RT format causes of differences | **v0.3** |
| `diagnose_mobile_risks` | diagnostic_tools | Comprehensive check of precision/performance/compatibility/GPU-specific risks, returning a prioritized risk list | **v0.3** |

---

## 4. Full Tool List (42 tools)

### Session (4)
| Tool | Description | Version |
|------|------|------|
| `open_capture` | Open an RDC file | v0.1 |
| `close_capture` | Close the current capture | v0.1 |
| `get_capture_info` | Basic capture info + automatic GPU pitfall detection | v0.1 (**v0.3 enhanced**) |
| `get_frame_overview` | Frame-level statistics: action counts by type, texture/buffer memory, RTs, resolution | **v0.2** |

### Events (5)
| Tool | Description | Version |
|------|------|------|
| `list_actions` | List the action tree (supports filter/event_type) | v0.1 (**v0.3 enhanced**) |
| `get_action` | Get details of a single action | v0.1 |
| `set_event` | Jump to a given event | v0.1 |
| `search_actions` | Search by name/flag | v0.1 |
| `find_draws` | Search by render state (blend/vertex count/texture/shader/RT) | **v0.2** |

### Pipeline (4)
| Tool | Description | Version |
|------|------|------|
| `get_pipeline_state` | Get pipeline state (enum values made readable) | v0.1 (v0.2 enhanced) |
| `get_shader_bindings` | Get shader resource bindings | v0.1 |
| `get_vertex_inputs` | Get vertex inputs | v0.1 |
| `get_draw_call_state` | Return the complete draw call state in one call | **v0.2** |

### Resources (4)
| Tool | Description | Version |
|------|------|------|
| `list_textures` | List textures | v0.1 |
| `list_buffers` | List buffers | v0.1 |
| `list_resources` | List all named resources | v0.1 |
| `get_resource_usage` | Get resource usage records | v0.1 |

### Data (8)
| Tool | Description | Version |
|------|------|------|
| `save_texture` | Save a texture to a file | v0.1 |
| `get_buffer_data` | Read buffer data | v0.1 |
| `pick_pixel` | Pick a pixel value | v0.1 |
| `get_texture_stats` | Texture statistics + anomaly detection (NaN/Inf/negative), supports all_slices | v0.1 (**v0.3 enhanced**) |
| `read_texture_pixels` | Read a rectangular region of pixels (up to 64×64) with per-pixel anomaly flags | **v0.3** |
| `export_draw_textures` | Batch export all textures bound to a draw call | **v0.2** |
| `save_render_target` | Save a snapshot of the current RT (color + optional depth) | **v0.2** |
| `export_mesh` | Export a mesh as OBJ (position/normal/uv) | **v0.2** |

### Shaders (3)
| Tool | Description | Version |
|------|------|------|
| `disassemble_shader` | Disassemble a shader (automatic fallback chain + reflection fallback), supports search/line_range | v0.1 (**v0.3 enhanced**) |
| `get_shader_reflection` | Get shader reflection info | v0.1 |
| `get_cbuffer_contents` | Read constant buffers (supports filter by variable) | v0.1 (**v0.3 enhanced**) |

### Advanced (6)
| Tool | Description | Version |
|------|------|------|
| `pixel_history` | Pixel history (passed field bug fixed) | v0.1 (v0.2 fix) |
| `get_post_vs_data` | Get VS output data | v0.1 |
| `diff_draw_calls` | Compare the state of two draw calls, with implication notes | v0.2 (**v0.3 enhanced**) |
| `analyze_render_passes` | Detect render pass boundaries automatically and summarize them | **v0.2** |
| `sample_pixel_region` | Sample an RT region on a uniform grid, detecting NaN/Inf/negative/overexposed hotspots | **v0.3** |
| `debug_shader_at_pixel` | Per-pixel shader debugging; when unsupported, falls back to returning the pixel value + reflection info | **v0.3** |

### Performance (4)
| Tool | Description | Version |
|------|------|------|
| `get_pass_timing` | Most expensive render passes (GPU counters / triangle-count heuristic) | **v0.3** |
| `analyze_overdraw` | Estimate overdraw grouped by RT | **v0.3** |
| `analyze_bandwidth` | Estimate write/read bandwidth per RT | **v0.3** |
| `analyze_state_changes` | Find redundant state changes and draw call batching opportunities | **v0.3** |

### Diagnostics (4)
| Tool | Description | Version |
|------|------|------|
| `diagnose_negative_values` | Scan float RTs for negative values/NaN/Inf, find the first event that introduces them, and detect TAA accumulation | **v0.3** |
| `diagnose_precision_issues` | Check for missing R11G11B10 sign bit, low-precision depth buffers, and SRGB/linear space mismatches | **v0.3** |
| `diagnose_reflection_mismatch` | Compare the reflection pass against the main scene and report the shader/blend/RT format causes of differences | **v0.3** |
| `diagnose_mobile_risks` | Comprehensive check of precision/performance/compatibility/GPU-specific risks, returning a prioritized risk list | **v0.3** |

---

## 5. Remaining Items

| # | Improvement | Description |
|---|------|------|
| 1 | User-level configuration docs + install script | Recommend `~/.claude/settings.json` configuration in the README |
| 7 (partial) | GL/GLES original GLSL source extraction | Extract the original GLSL source from the structured file |
| 8 | Uniform value read fix | Support multiple API versions and add a high-level `get_uniform_values()` tool |
| New | Extend tests/test_util.py | Add mock tests for performance_tools / diagnostic_tools |
