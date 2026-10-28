# renderdoc-mcp

<div align="center">

<h2>⚠️ Disclaimer</h2>

$\Large\color{#ff69b4}{\textsf{This is a personal }}$ $\Large\color{#ff69b4}{\mathtt{✨ vibe\text{-}coding ✨}}$ $\Large\color{#ff69b4}{\textsf{project —}}$
$\Large\color{#ff69b4}{\textsf{built for fun, not for production use.}}$

$\small\color{gray}{\textsf{I have no idea why this repo has so many stars...}}$

<img src="assets/crying.gif" width="64" />

</div>

MCP server for [RenderDoc](https://renderdoc.org/) — let AI assistants analyze GPU frame captures (`.rdc` files) for graphics debugging and performance analysis.

Built on the [Model Context Protocol](https://modelcontextprotocol.io/), works with Claude Desktop, Claude Code, and any MCP-compatible client.

## Features

- **42 tools** covering the full RenderDoc analysis workflow
- **10 high-level tools** for one-call analysis (draw call state, frame overview, diff, batch export, pixel region sampling, etc.)
- **3 built-in prompts** for guided debugging
- **Human-readable output** — blend modes, depth functions, topology shown as names not numbers
- **GPU quirk detection** — auto-identifies Adreno/Mali/PowerVR/Apple-specific pitfalls from driver name
- **Headless** — no GUI needed, runs entirely via RenderDoc's Python replay API
- **Pure Python** — single `pip install`, no build step
- Supports D3D11, D3D12, OpenGL, Vulkan, OpenGL ES captures

## Quick Start

### 1. Prerequisites

- **Python 3.10+**
- **RenderDoc** installed ([download](https://renderdoc.org/builds))
  - You need the `renderdoc.pyd` (Windows) or `renderdoc.so` (Linux) Python module
  - It ships with every RenderDoc installation

### 2. Install

```bash
git clone https://github.com/Linkingooo/renderdoc-mcp.git
cd renderdoc-mcp
pip install -e .
```

### 3. Find your `renderdoc.pyd` path

The Python module is in your RenderDoc install directory:

| Platform | Typical path |
|----------|-------------|
| Windows  | `C:\Program Files\RenderDoc\renderdoc.pyd` |
| Linux    | `/usr/lib/renderdoc/librenderdoc.so` or where you built it |

You need the **directory** containing this file.

### 4. Configure your MCP client

<details>
<summary><b>Claude Desktop</b></summary>

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "python",
      "args": ["-m", "renderdoc_mcp"],
      "env": {
        "RENDERDOC_MODULE_PATH": "C:\\Program Files\\RenderDoc"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Claude Code</b></summary>

Add to `.claude/settings.json`:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "python",
      "args": ["-m", "renderdoc_mcp"],
      "env": {
        "RENDERDOC_MODULE_PATH": "C:\\Program Files\\RenderDoc"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Run standalone</b></summary>

```bash
# Set the module path
export RENDERDOC_MODULE_PATH="/path/to/renderdoc"   # Linux/macOS
set RENDERDOC_MODULE_PATH=C:\Program Files\RenderDoc  # Windows

# Run
python -m renderdoc_mcp
```

</details>

## Usage Examples

Once configured, just talk to your AI assistant:

> "Open `frame.rdc` and show me what's happening in the frame"

> "Find the draw call that renders the character model and check its pipeline state"

> "Why is my shadow map rendering black? Check the depth pass"

> "Analyze performance — are there any redundant draw calls?"

### Typical Tool Flow

```
open_capture("frame.rdc")                     # Load the capture
├── get_capture_info()                         # API, GPU, known_gpu_quirks
├── get_frame_overview()                       # Frame-level stats and render passes
├── get_draw_call_state(142)                   # Complete draw call state in one call
├── diff_draw_calls(140, 142)                  # Compare two draw calls (with implications)
├── export_draw_textures(142, "./tex/")        # Batch export all bound textures
├── save_render_target(142, "./rt.png")        # Save render target snapshot
├── analyze_render_passes()                    # Auto-detect render pass boundaries
├── find_draws(blend=True, min_vertices=1000)  # Search by rendering state
├── sample_pixel_region(rt_id, 0,0,512,512)   # Scan RT region for NaN/Inf/negatives
├── pixel_history(id, 512, 384)               # Debug a specific pixel
├── export_mesh(142, "./mesh.obj")             # Export mesh as OBJ
└── close_capture()                            # Clean up
```

For performance and diagnostic analysis:

```
get_pass_timing(granularity="pass")      # Find most expensive render passes
analyze_overdraw()                        # Fill-rate pressure estimate
analyze_bandwidth()                       # Memory bandwidth estimate
analyze_state_changes()                   # Batching opportunities
diagnose_negative_values()               # Find NaN/Inf/negative color values (bright flashes)
diagnose_precision_issues()              # R11G11B10, D16, SRGB mismatches
diagnose_reflection_mismatch()           # Reflection artifact diagnosis
diagnose_mobile_risks()                  # Comprehensive mobile GPU risk check
```

For lower-level inspection, all granular tools remain available:

```
set_event(142)                                    # Navigate to a draw call
├── get_pipeline_state()                          # Inspect rasterizer/blend/depth
├── get_shader_bindings("pixel")                  # Check what textures/buffers are bound
├── get_cbuffer_contents("pixel", 0, filter="ibl") # Read shader constants (filterable)
├── disassemble_shader("pixel", search="SampleSH") # Shader code with context search
└── save_texture(id, "rt.png")                    # Export a specific texture
```

## Tools

### Session Management (4)

| Tool | Description |
|------|-------------|
| `open_capture` | Open a `.rdc` file (auto-closes previous) |
| `close_capture` | Close current capture and free resources |
| `get_capture_info` | Capture metadata: API, action count, resolution, **known_gpu_quirks** (Adreno/Mali/PowerVR/Apple) |
| `get_frame_overview` | **Frame-level statistics**: action counts by type, texture/buffer memory, render targets, resolution |

### Event Navigation (5)

| Tool | Description |
|------|-------------|
| `list_actions` | List the draw call / action tree — supports `filter` (name substring) and `event_type` (draw/clear/copy…) |
| `get_action` | Full detail for a single action |
| `set_event` | Navigate to an event (**required** before pipeline queries) |
| `search_actions` | Search by name pattern and/or action flags |
| `find_draws` | **Search draw calls by rendering state**: blend, min vertices, texture/shader/RT binding |

### Pipeline Inspection (4)

| Tool | Description |
|------|-------------|
| `get_pipeline_state` | Full state: topology, viewports, rasterizer, blend, depth, stencil (human-readable enums) |
| `get_shader_bindings` | Constant buffers, SRVs, UAVs, samplers for a shader stage |
| `get_vertex_inputs` | Vertex attributes, vertex/index buffer bindings |
| `get_draw_call_state` | **One-call draw analysis**: action info, blend formula, depth, stencil, rasterizer, textures with sizes, RTs, shaders |

### Resource Analysis (4)

| Tool | Description |
|------|-------------|
| `list_textures` | All textures (filterable by format, min width) |
| `list_buffers` | All buffers (filterable by min size) |
| `list_resources` | All named resources (filterable by type, name pattern) |
| `get_resource_usage` | Which events read/write a resource |

### Data Extraction (8)

| Tool | Description |
|------|-------------|
| `save_texture` | Export to PNG, JPG, BMP, TGA, HDR, EXR, or DDS |
| `get_buffer_data` | Read buffer bytes (hex dump or float32 array) |
| `pick_pixel` | RGBA value at a coordinate |
| `get_texture_stats` | Per-channel min/max/avg with **anomaly detection** (NaN/Inf/negative); supports `all_slices` for cubemaps |
| `read_texture_pixels` | Read a rectangular region of pixels (up to 64×64) with per-pixel anomaly flags |
| `export_draw_textures` | **Batch export** all textures bound to a draw call (auto-names, skips placeholders) |
| `save_render_target` | **Save RT snapshot** at an event (color + optional depth) |
| `export_mesh` | **Export mesh as OBJ** with positions, normals, UVs from post-VS data |

### Shader Analysis (3)

| Tool | Description |
|------|-------------|
| `disassemble_shader` | Shader disassembly with **auto fallback chain**; supports `search` (keyword + context) and `line_range` |
| `get_shader_reflection` | Input/output signatures, resource binding layout |
| `get_cbuffer_contents` | Actual constant buffer variable values; supports `filter` for variable name substring |

### Advanced (6)

| Tool | Description |
|------|-------------|
| `pixel_history` | Full per-pixel modification history across all events |
| `get_post_vs_data` | Post-transform vertex data (VS out / GS out) |
| `diff_draw_calls` | **Compare two draw calls** — shows state differences with human-readable implications |
| `analyze_render_passes` | **Auto-detect render pass boundaries** by Clear/RT switches, summarize each pass |
| `sample_pixel_region` | **Uniform-grid scan** of an RT region — detects NaN/Inf/negative/overexposed hotspots |
| `debug_shader_at_pixel` | **Per-pixel shader debug** — returns variable trace or pixel value + shader info as fallback |

### Performance Analysis (4)

| Tool | Description |
|------|-------------|
| `get_pass_timing` | Most expensive render passes — uses GPU counters if available, falls back to triangle-count heuristic |
| `analyze_overdraw` | Overdraw estimate per render target group |
| `analyze_bandwidth` | Write/read bandwidth estimate per render target |
| `analyze_state_changes` | Finds redundant state-change patterns and batching opportunities |

### Diagnostics (4)

| Tool | Description |
|------|-------------|
| `diagnose_negative_values` | Scans all float RTs for negative/NaN/Inf — finds first event introducing them, detects TAA accumulation |
| `diagnose_precision_issues` | Checks R11G11B10 sign-bit loss, shallow depth buffers, SRGB/linear mismatches |
| `diagnose_reflection_mismatch` | Compares reflection passes against main scene draws — finds shader/blend/format causes |
| `diagnose_mobile_risks` | Comprehensive check across precision / performance / compatibility / GPU-specific risk categories |

## Prompts

Built-in prompt templates to guide AI through common workflows:

| Prompt | Description |
|--------|-------------|
| `debug_draw_call` | Deep-dive a single draw call: pipeline → shaders → cbuffers → outputs |
| `find_rendering_issue` | Systematic diagnosis from a problem description |
| `analyze_performance` | Frame-wide perf analysis: pass timing, overdraw, bandwidth, state changes |

## How It Works

```
AI Assistant ←—MCP—→ renderdoc-mcp server ←—Python API—→ renderdoc.pyd ←→ GPU replay
```

The server uses RenderDoc's headless replay API (`renderdoc.pyd`) to:
1. Open `.rdc` capture files without the GUI
2. Replay frames and query pipeline state at any event
3. Extract textures, buffers, shader data, and pixel history
4. Return structured JSON for the AI to reason about

## Development

```bash
# Install in dev mode
pip install -e .

# Run tests (no RenderDoc needed — uses mocks)
python -m pytest tests/ -v

# Project structure
src/renderdoc_mcp/
├── server.py                 # FastMCP server, 3 prompt definitions
├── session.py                # Capture lifecycle, resource/texture caches (singleton)
├── util.py                   # Serialization, enum maps, blend formula, module loader
└── tools/
    ├── session_tools.py      # open/close/info (GPU quirks) + get_frame_overview
    ├── event_tools.py        # list/get/set/search actions + find_draws
    ├── pipeline_tools.py     # pipeline state, shader bindings, vertex inputs + get_draw_call_state
    ├── resource_tools.py     # texture/buffer/resource enumeration
    ├── data_tools.py         # save/read/pick/stats + read_texture_pixels + export_draw_textures, save_render_target, export_mesh
    ├── shader_tools.py       # disassembly (fallback chain, search), reflection, cbuffer contents (filter)
    ├── advanced_tools.py     # pixel history, post-VS data + diff_draw_calls (implications), analyze_render_passes, sample_pixel_region, debug_shader_at_pixel
    ├── performance_tools.py  # get_pass_timing, analyze_overdraw, analyze_bandwidth, analyze_state_changes
    └── diagnostic_tools.py  # diagnose_negative_values, diagnose_precision_issues, diagnose_reflection_mismatch, diagnose_mobile_risks
```

## License

MIT
