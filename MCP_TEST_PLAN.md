# RenderDoc MCP Tool Test Report

## Test Capture File

- **Path**: `C:\Users\18523\Downloads\5.rdc`
- **API**: OpenGL ES
- **Resolution**: 1496x720
- **Total Actions**: 285 (266 draw calls, 18 clears, 0 dispatches)
- **Textures**: 172, 139.73 MB
- **Buffers**: 141, 21.72 MB
- **Render Passes**: 18
- **Main RT**: `ResourceId::4749` (1496x720, R8G8B8A8_UNORM, 151 draws)

## Key Event ID Reference

| EID | Type | Vertices | Notes |
|-----|------|--------|------|
| 24 | Clear | - | glClear Color+Depth+Stencil |
| 111 | Draw | 59148 | Large draw, pass #2 |
| 569 | Draw | 83829 | One of the largest draws, TriangleStrip, VS 5 inputs |
| 825 | Draw | 83829 | Same vertex count but different shader (can be diffed with 569) |
| 855 | Clear | - | Start of main pass (#6), clear color=(0.178,0.203,0.25) |
| 1014 | Draw | 59148 | Large draw within the main pass |
| 3076 | Draw | 1920 | UI-related BaseVertex draw |
| 3125 | Draw | - | Last draw of the main pass (#6) |
| 3346 | Draw | 6 | Post-processing full-screen quad |
| 3622 | Draw | 6924 | Draw within the final pass |

## Test Flow and Results

### Phase 1: Basic Tools

| # | Tool | Test parameters | Expected | Status |
|---|------|----------|------|------|
| 1 | `open_capture` | filepath=`C:\Users\18523\Downloads\5.rdc` | Returns API/action/texture/buffer counts | ✅ |
| 2 | `get_capture_info` | (no parameters) | Returns current capture info | ✅ |
| 3 | `get_frame_overview` | (no parameters) | Returns resolution/draw stats/RT list | ✅ |
| 4 | `list_actions` | max_depth=2, filter_flags=["Drawcall","Clear"] | Filters to draws+clears | ✅ |
| 5 | `search_actions` | name_pattern="BaseVertex" | Finds all BaseVertex calls | ✅ (111 matches) |
| 6 | `get_action` | event_id=569 | Returns details of a single action | ✅ (Bug 3 fix verified) |
| 7 | `set_event` | event_id=569 | Navigates successfully | ✅ |

### Phase 2: Draw Call Analysis

| # | Tool | Test parameters | Expected | Status |
|---|------|----------|------|------|
| 8 | `get_draw_call_state` | event_id=569 | Full draw state (including RT, blend, shader, etc.) | ✅ (RT works after Bug 2 fix; EID 569 is a depth-only pass) |
| 9 | `diff_draw_calls` | eid1=569, eid2=825 | Lists differences (shader/inputs differ) | ✅ |
| 10 | `get_pipeline_state` | event_id=569 | Returns full pipeline state | ✅ (EID 569 is depth-only, so empty output_targets is correct; EID 3346 has an RT and works) |
| 11 | `get_vertex_inputs` | event_id=569 | Returns vertex attributes and buffer bindings | ✅ (Bug 4 fixed; format shows R32G32B32_FLOAT etc.) |
| 12 | `find_draws` | min_vertices=50000, max_results=5 | Finds 5 large draws | ✅ |
| 13 | `find_draws` | blend=True | Finds draws with blend enabled | ✅ (0 matches, expected) |
| 14 | `analyze_render_passes` | (no parameters) | Detects 18 render passes | ✅ |

### Phase 3: Shader Analysis

| # | Tool | Test parameters | Expected | Status |
|---|------|----------|------|------|
| 15 | `disassemble_shader` | stage="vertex", event_id=569 | Disassembly (may fail on ES → fallback) | ⚠️ SPIR-V compilation failed, known limitation |
| 16 | `get_shader_reflection` | stage="pixel", event_id=569 | Returns IO signature, cbuffers, resource bindings | ✅ (Bug 1 fix verified, 4 inputs/1 output/1 cbuffer) |
| 17 | `get_shader_reflection` | stage="vertex", event_id=569 | Returns VS reflection info | ✅ (5 inputs, 5 outputs, 1 cbuffer) |
| 18 | `get_shader_bindings` | stage="pixel", event_id=569 | Returns PS resource bindings | ✅ (_DissolveMap returns ResourceId::3198 correctly) |
| 19 | `get_cbuffer_contents` | stage="vertex", cbuffer_index=0, event_id=569 | Returns cbuffer variable values | ✅ (Unity matrices etc., 6 variables) |

### Phase 4: Resource Queries

| # | Tool | Test parameters | Expected | Status |
|---|------|----------|------|------|
| 20 | `list_textures` | (no parameters) | Lists all 172 textures | ✅ (172 textures) |
| 21 | `list_textures` | min_width=512 | Filters large textures | ✅ (108 textures) |
| 22 | `list_buffers` | min_size=100000 | Filters large buffers | ✅ (23 buffers) |
| 23 | `list_resources` | name_pattern=".*" | Lists all named resources | ✅ (Bug 5 fix verified, full resource list returned) |
| 24 | `get_resource_usage` | resource_id="ResourceId::4749" | Usage history of the main RT | ✅ (155 usages; usage now shows readable names such as VS_RWResource) |

### Phase 5: Data Export

| # | Tool | Test parameters | Expected | Status |
|---|------|----------|------|------|
| 25 | `save_texture` | resource_id=ResourceId::4749, output=Downloads | Exports the texture as PNG | ✅ |
| 26 | `get_buffer_data` | resource_id=ResourceId::4026, format="floats" | Reads buffer float data | ✅ (returned vertex float data) |
| 27 | `export_draw_textures` | event_id=569, output_dir=Downloads/mcp_test_textures | Batch-exports PS textures | ✅ (0 textures, this draw has none) |
| 28 | `export_draw_textures` | event_id=1014, output_dir=Downloads/mcp_retest_textures | Retest with a draw that has textures | ✅ (6 textures exported: BumpMap/FlocculeMap/MainTex/OpalMap/ReflectionMatCap/Lightmap) |
| 29 | `save_render_target` | event_id=3125, output=Downloads/mcp_test_rt.png | Saves the final RT of the main pass | ✅ (works after fix) |
| 30 | `save_render_target` | event_id=3125, save_depth=True | Also saves depth | ✅ (color + depth, two files) |
| 31 | `export_mesh` | event_id=569, output=Downloads/mcp_test_mesh.obj | Exports the mesh as OBJ | ✅ (18218 vertices, 6072 triangles) |

### Phase 6: Pixel and Advanced Analysis

| # | Tool | Test parameters | Expected | Status |
|---|------|----------|------|------|
| 32 | `pick_pixel` | resource_id="ResourceId::4749", x=748, y=360 | Reads the center pixel RGBA | ✅ (R=0.435, G=0.325, B=1.0, A=1.0) |
| 33 | `get_texture_stats` | resource_id="ResourceId::4749" | Texture stats min/max/avg | ✅ (min/max fine, no avg) |
| 34 | `pixel_history` | resource_id="ResourceId::4749", x=748, y=360 | Pixel modification history | ✅ (13 modifications) |
| 35 | `get_post_vs_data` | event_id=569, stage="vsout", max_vertices=10 | Post-VS vertex data | ✅ (10/83829 vertices, 5 attrs) |

### Phase 7: Session Management

| # | Tool | Test parameters | Expected | Status |
|---|------|----------|------|------|
| 36 | `close_capture` | (no parameters) | Closes and releases resources | ✅ (closed normally; later calls return NO_CAPTURE_OPEN) |

### Phase 8: New Tool Verification (P1/P2)

| # | Tool | Test parameters | Expected | Status |
|---|------|----------|------|------|
| 37 | `read_texture_pixels` | resource_id="ResourceId::4749", x=744, y=358, w=8, h=8, eid=3125 | Returns an 8×8 RGBA pixel grid | ✅ (64 pixels returned, including blue sky region values) |
| 38 | `sample_pixel_region` | resource_id="ResourceId::4749", eid=3125, sample_count=256 | Batch sampling + anomaly detection | ✅ (253 samples, 0 anomalies, RGB mean: 0.41/0.42/0.67) |
| 39 | `get_pass_timing` | granularity="pass", top_n=10 | Grouped by pass + real GPU timing | ✅ (has_real_timing=true! Main pass 855-3125 is the slowest at 0.021ms) |
| 39b | `get_pass_timing` | granularity="draw_call", top_n=5 | Top-5 most expensive draws | ✅ (EID 2155 is the most expensive at 0.002ms, 32K vertices) |
| 40 | `analyze_overdraw` | (no parameters) | Frame-level overdraw analysis | ✅ (main RT 151 draws, main_resolution=1496x720 correct; passed after restart following Bug 6 + improvement #7 fixes) |
| 41 | `analyze_state_changes` | change_types=["shader","blend","depth","render_target"] | State-change stats + batching opportunities | ✅ (shader switches 114/200; found 18 instanceable draws at EID 3017-3042) |
| 42 | `debug_shader_at_pixel` | event_id=3346, x=748, y=360 | Shader trace or fallback | ✅ (ES doesn't support trace; fallback returns pixel value + shader info) |
| 43 | `diagnose_reflection_mismatch` | (no parameters, auto-detect) | No reflection pass found → report suggestion | ✅ (NO_REFLECTION_PASS_FOUND, suggests using list_actions to look) |
| 44 | `diagnose_negative_values` | (no parameters) | Scans float RTs for negative values | ✅ (NO_ANOMALIES_FOUND, no negative/NaN/Inf values) |
| 45 | `diagnose_mobile_risks` | check_categories=["performance","gpu_specific"] | Mobile risk assessment | ✅ (1 risk: too many full-screen passes (7 clears), medium; device=OpenGLES) |
| 46 | `diagnose_precision_issues` | (no parameters) | Precision risk check | ✅ (5 issues: 2 medium D16 + 3 low D24 depth precision) |
| 47 | `diagnose_mobile_risks` | (all categories) | Full risk assessment | ✅ (3 risks: 2 high D16 precision + 1 medium: too many full-screen passes) |
| 48 | `analyze_bandwidth` | breakdown_by="pass" | Bandwidth estimate | ✅ (total bandwidth ~1132 MB, main RT 637 MB, includes tile warning) |

## Test Summary

- **Total**: 48 items (36 basic and 12 new tools).
  #39 has a 39b variant.
- **✅ Passed**: 47 items, including 6 that passed after bug fixes.
  Bug 6 + improvement #7 passed after restart.
- **⏳ Pending verification**: 2 items, #15 `disassemble_shader` and #42 `debug_shader_at_pixel`.
  They need a Vulkan/D3D capture file.
- **⚠️ Known limitation**: OpenGL ES doesn't support shader trace/disassembly.
  Only the fallback path has been verified.

## Items Pending Verification (need a non-ES capture file)

| # | Tool | Notes | Required API |
|---|------|------|-----------|
| 15 | `disassemble_shader` | Shader disassembly; SPIR-V compilation fails on ES | Vulkan / D3D11 / D3D12 |
| 42 | `debug_shader_at_pixel` | Pixel-level shader debug trace; not supported on ES | Vulkan / D3D11 / D3D12 |

## Bugs Found and Fixed

### Bug 1: ShaderResource.resType does not exist
- **Error**: `'renderdoc.ShaderResource' object has no attribute 'resType'`
- **Cause**: In the RenderDoc API, `ShaderResource` uses `textureType` rather than `resType`
- **Fix**: `resType` → `textureType`
- **Affected files**:
  - `shader_tools.py` (4 places: lines 30, 31, 170, 180)
  - `pipeline_tools.py` (2 places: lines 252, 272)

### Bug 2: Descriptor.resourceId does not exist
- **Error**: `'renderdoc.Descriptor' object has no attribute 'resourceId'`
- **Cause**: `GetOutputTargets()` and `GetDepthTarget()` return `Descriptor` objects.
  Their resource ID attribute is `resource` rather than `resourceId`.
- **Note**: `TextureDescription.resourceId`, `BufferDescription.resourceId`, `ShaderReflection.resourceId`, `BoundVBuffer.resourceId`, `TextureSave.resourceId`, etc. are still correct
- **Fix**: `resourceId` → `resource` on Descriptor objects only
- **Affected files**:
  - `data_tools.py` (6 places: lines 319, 326, 337, 354, 355, 363)
  - `pipeline_tools.py` (6 places: lines 156×2, 163, 164, 497, 499, 513, 514)

### Bug 3: ActionDescription.depthOutput does not exist (newly found)
- **Error**: `'renderdoc.ActionDescription' object has no attribute 'depthOutput'`
- **Cause**: In the RenderDoc API, the attribute is named `depthOut` rather than `depthOutput`
- **Fix**: `depthOutput` → `depthOut`
- **Affected files**:
  - `util.py` (4 places: lines 210, 212, 246, 247)
  - `tests/test_util.py` (2 places: lines 126, 127)

### Bug 4: get_vertex_inputs format shows a Swig pointer (newly found)
- **Error**: The format field shows `<Swig Object of type 'ResourceFormat *' at 0x...>` instead of a readable format name
- **Cause**: `str(a.format)` can't produce a format name for the Swig-wrapped `ResourceFormat` object
- **Fix**: `str(a.format)` → `str(a.format.Name())`
- **Affected files**:
  - `pipeline_tools.py` (1 place: line 350)

### Bug 5: list_resources UTF-8 decode error (newly found)
- **Error**: `'utf-8' codec can't decode byte 0x90 in position 0: invalid start byte`
- **Cause**: Some resource names contain non-UTF-8 bytes.
  They may be binary data.
- **Fix**: Add try/except encoding protection around `res.name`
- **Affected files**:
  - `resource_tools.py` (lines 86-90)

### Bug 6: TextureDescription.name attribute does not exist (newly found)
- **Error**: `'renderdoc.TextureDescription' object has no attribute 'name'`
- **Cause**: The `TextureDescription` objects returned by `GetTextures()` have no `.name` attribute (unlike `ResourceDescription.name`)
- **Note**: `serialize_texture_desc()` is already guarded with `hasattr`, as is `data_tools.py:236`.
  The new tools access it directly.
- **Fix**: `tex.name → getattr(tex, 'name', None)` combined with `or fallback`
- **Affected files**:
  - `advanced_tools.py` (1 place: line 176)
  - `performance_tools.py` (1 place: line 348)
  - `diagnostic_tools.py` (8 places: lines 160, 292, 318, 326, 609, 615, 627, 703)
- **Status**: ✅ Code fixed, verified after restart

## Improvements (Non-Bugs)

1. ~~**get_resource_usage**: the `usage` field shows raw enum numbers~~ → ✅ Fixed.
   ISSUES #10 enum mapping.
   It now shows VS_RWResource etc.
2. ~~**get_shader_reflection**: `var_type`/`system_value` show numbers~~ → ✅ Fixed (ISSUES #10)
3. ~~**get_shader_bindings**: reading the PS _DissolveMap texture binding fails~~ → ✅ Fixed.
   Knock-on effect of Bug 2.
4. **pick_pixel**: `rgba_uint` returns the raw memory representation of the float rather than uint8 values
5. ~~**export_draw_textures**: EID 1014 exports 0 textures~~ → ✅ Fixed.
   Knock-on effect of Bug 2.
   It now exports 6 textures.
6. **get_texture_stats**: only returns min/max, no avg/mean statistics
7. ~~**analyze_overdraw**: `main_resolution` takes the first RT~~ → ✅ Fixed.
   It now scans all draws for the largest RT.
   Verified after restart: main_resolution=1496x720.

## Environment Limitations

- **Shader disassembly**: SPIR-V compilation requires ES 310+.
  The ES shader version in the current capture doesn't meet it.
  AMD GCN ISA needs an older AMD driver (<22.7.1).
- **renderdoc module**: Only available in RenderDoc's embedded Python environment.
  It cannot be imported from an external Python.

## Exported File Paths

Files exported during testing:
- Mesh: `C:\Users\18523\Downloads\mcp_test_mesh.obj`
- RT color: `C:\Users\18523\Downloads\mcp_test_rt.png`
- RT depth: `C:\Users\18523\Downloads\mcp_test_rt_depth_depth`
- Texture 4749: `C:\Users\18523\Downloads\mcp_test_texture_4749.png`
- Retest textures (6 files): `C:\Users\18523\Downloads\mcp_retest_textures\`
