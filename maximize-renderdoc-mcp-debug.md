# Maximizing RenderDoc MCP Debugging: Companion Tools and Integration Strategy

---

## The Core Question: What RenderDoc MCP Can and Cannot Do Alone

RenderDoc is essentially a **frame-level snapshot analyzer**.
It can tell you "what happened in this frame".
But it has three fundamental blind spots:

```
What RenderDoc can do                     What RenderDoc cannot do
─────────────────────                     ─────────────────────
✅ The draw call list for this frame      ❌ Why the engine issued this draw call
✅ The current color value of a pixel     ❌ How that color was computed from material parameters (engine level)
✅ The final compiled shader code         ❌ The original .usf/.ush source before compilation
✅ Values in constant buffers             ❌ Which engine module set those values, and where
✅ GPU time for this frame                ❌ Cross-frame performance trends and CPU-side bottlenecks
✅ RT formats and pixel data              ❌ Why this format was chosen (engine configuration)
✅ Anomaly detection within a frame       ❌ Whether the anomaly reproduces reliably, and on which devices
✅ Finding problems                       ❌ Modifying code to fix them
```

These blind spots mean that **RenderDoc MCP alone can do roughly 60-70% of the diagnostic work**.
That work is finding the problem and locating its cause at the shader / pipeline state level.

**The remaining 30-40% needs other tools.**
It consists of:

- Understanding the engine-level why
- Fixing the code
- Verifying the fix

---

## Companion Overview

Companions are listed in priority order.
Each covers one specific RenderDoc blind spot:

```
Priority  Companion MCP        Blind spot covered                           Value
─────────────────────────────────────────────────────────────────────────────────
P0        Filesystem MCP       Read/write project source, edit shaders      Closes the loop from diagnosis to fix
P0        Git MCP              Version comparison, committing fixes         Finds when the problem was introduced
P1        UE Editor MCP        Engine-level material/render settings        Understanding the "why"
P1        ADB / Device MCP     Device management, capture triggers, logs    Multi-device repro and verification
P2        Performance Log MCP  Cross-frame performance data, CPU profiling  Adds a view beyond GPU-only
P2        Knowledge Base MCP   GPU hardware quirks, known-bug database      Aids root-cause reasoning
```

Each is covered in detail below.

---

## P0 Filesystem MCP: Closing the Loop from "Finding" to "Fixing"

### Why P0

RenderDoc MCP's biggest limitation is that it is **read-only**.
It can tell you "the SH sampling on line 142 of the IBL shader produces negative values; add max(0, x)".
But it cannot change the code.
If you have to find the file and edit the code by hand after Claude Code diagnoses the problem, the workflow loses much of its automation.

### Implementation

No separate development is needed, since Claude Code already has file system access.
The key is for RenderDoc MCP return values to carry enough information for Claude Code to locate the source file:

```
RenderDoc returns                           Claude Code's next steps
─────────────────                           ─────────────────────
shader: "MobileBasePass.usf", line 142      → Search the project for MobileBasePass.usf
                                            → Go to line 142
                                            → Add a max(0, iblDiffuse) clamp
                                            → Commit the change
```

### Required RenderDoc MCP Changes

In the return values of `get_shader_source` and `debug_shader_at_pixel`, keep the original file name and line mapping where possible:

```json
{
  "source_file_hint": "Engine/Shaders/Private/MobileBasePassPixelShader.usf",
  "line_mapping": {
    "compiled_line_142": "source_line_387 in MobileBasePassPixelShader.usf"
  }
}
```

> Note: Line numbers in decompiled SPIR-V do not necessarily match the original HLSL/GLSL.
> If the RenderDoc capture includes debug info (shaders compiled with -g or -Od), you get an exact mapping.
> Otherwise you can only match by function name and code pattern.
> Always enable shader debug info in the capture flow during development.

### Typical Closed-Loop Flow

```
1. [RenderDoc MCP] diagnose_negative_values()
   → "SH sampling on line 142 of the IBL_Apply shader (MobileBasePass.usf) produces negative values"

2. [Filesystem] Search the project for MobileBasePassPixelShader.usf
   → Finds Engine/Shaders/Private/MobileBasePassPixelShader.usf

3. [Filesystem] Read the file and locate the SampleSH call

4. [Claude Code reasoning] Generate fix options:
   - Option A: add max(0, result) after SH sampling
   - Option B: clamp during SH coefficient preprocessing

5. [Filesystem] Write the change

6. [Git MCP] Commit to the fix/ibl-negative-clamp branch
```

---

## P0 Git MCP: Tracing When a Problem Was Introduced and Managing Fixes

### Why P0

A very common question when chasing rendering bugs is: **"When was this bug introduced?"**
For example, "the reflection was fine last week, and this week it is suddenly darker".
Answering it means comparing shader code and rendering configuration across versions.

### Key Tools

```json
{
  "name": "git_log_file",
  "description": "View the change history of a given file"
},
{
  "name": "git_diff",
  "description": "Compare file differences between two commits"
},
{
  "name": "git_blame",
  "description": "View who last changed each line of a file, and when"
},
{
  "name": "git_commit",
  "description": "Commit changes to version control"
},
{
  "name": "git_branch",
  "description": "Create/switch branches"
}
```

### Typical Flow

```
1. [RenderDoc MCP] diagnose_reflection_mismatch()
   → "The reflection pass uses the REFLECTION=1 variant, which lacks IBL specular"

2. [Git MCP] git_log_file("MobileBasePassPixelShader.usf")
   → Finds a change from 3 days ago: "Optimization: remove IBL specular from reflection pass to improve performance"

3. [Git MCP] git_diff(commit_before, commit_after, file="MobileBasePassPixelShader.usf")
   → Confirms which lines were deleted

4. [Claude Code reasoning]
   → "The performance optimization 3 days ago removed IBL specular from the reflection pass, lowering reflection brightness by about 15%.
      Suggestion: don't remove it entirely; use a simplified IBL specular instead (e.g., sample only the highest mip of the cubemap)"

5. [Filesystem] Write the change
6. [Git MCP] Commit to the fix/reflection-brightness branch
```

---

## P1 UE Editor MCP: Understanding the Engine-Level "Why"

### Why P1

RenderDoc sees the instructions the GPU finally executes.
But the root cause of many problems lies in engine configuration.
Some examples:

| What RenderDoc sees | Actual engine-level cause |
|--------------------|--------------------|
| The reflection RT resolution is only 1/4 of the main RT | The SceneCapture component's resolution scale was set to 0.25 |
| The REFLECTION=1 shader variant lacks IBL | The material's "Used with Planar Reflection" switch triggers a different compile path |
| R11G11B10_FLOAT instead of R16F | The project's Mobile HDR setting uses the "Mosaic" encoding mode |
| A post-process effect is unexpectedly enabled | A PostProcessVolume blend weight problem |
| Abnormally high TAA feedback weight | Custom TAA parameters were changed by mistake in project settings |

Without a UE Editor MCP, Claude Code can only guess at engine-level causes.
With one, it can check directly.

### Key Tools

```json
{
  "name": "query_material",
  "description": "Query a material instance's parameter values, shader compile configuration, and feature flags in use"
},
{
  "name": "query_render_settings",
  "description": "Query the project's render settings: Mobile HDR mode, TAA parameters, shadow configuration, etc."
},
{
  "name": "query_scene_component",
  "description": "Query parameters of specific scene components (SceneCapture, PostProcessVolume, Light)"
},
{
  "name": "query_console_variables",
  "description": "Query engine console variable values (r.MobileHDR, r.Tonemapper, etc.)"
},
{
  "name": "list_shader_permutations",
  "description": "List all shader variants compiled for a material and their trigger conditions"
}
```

### Typical Flow (Full Investigation of a Dark Reflection)

```
1. [RenderDoc MCP] diagnose_reflection_mismatch()
   → "The reflection pass shader variant REFLECTION=1 lacks IBL specular, and blending uses SrcAlpha"
   → "The reflection RT resolution is 1/4 of the main RT"

2. [UE Editor MCP] query_scene_component("PlanarReflection_01")
   → Confirms ScreenPercentage=25 (the source of the 1/4)

3. [UE Editor MCP] query_material("M_Building_01")
   → Confirms the material enables "Used with Planar Reflection"
   → Checks for a custom reflection brightness compensation parameter

4. [UE Editor MCP] list_shader_permutations("M_Building_01")
   → Compares compile option differences between the REFLECTION=0 and REFLECTION=1 variants
   → Confirms REFLECTION=1 does remove IBL specular

5. [Claude Code reasoning] Combine RenderDoc frame data with engine configuration:
   → "The problem has two compounding causes:
      a) The shader's REFLECTION=1 variant removes IBL specular (-15% brightness)
      b) PlanarReflection blend uses SrcAlpha with alpha=0.85 (another -15%)
      Total -28% brightness, matching the measurement."

6. [Filesystem] Edit the shader's REFLECTION branch to keep a simplified IBL
7. [UE Editor MCP] Change the PlanarReflection blend parameters (if needed)
8. [Git MCP] Commit the fix
```

### Alternative When No UE Editor MCP Is Available

If the UE Editor MCP is not implemented yet, Claude Code can still get some of this information.
It reads config files directly through Filesystem:

```
- Project render settings  → [/Script/Engine.RendererSettings] in Config/DefaultEngine.ini
- Console variables        → Config/DefaultEngine.ini or DefaultDeviceProfiles.ini
- Material information     → Requires parsing .uasset (binary format, hard to read directly)
```

This covers about 50% of engine configuration queries.
Material and scene component queries have to wait for the UE Editor MCP.

---

## P1 ADB / Device MCP: Multi-Device Repro and Verification

### Why P1

A core difficulty with mobile rendering bugs is **device variance**.
The same frame can behave differently across devices:

- It shows bright flashes on Adreno.
- It looks fine on Mali.
- It reproduces only on a particular driver version.

You need to be able to:

1. Trigger captures on multiple devices
2. Collect GPU logs and error information from different devices
3. Verify the fix on the target device

### Key Tools

```json
{
  "name": "adb_list_devices",
  "description": "List all connected Android devices and their GPU information"
},
{
  "name": "adb_capture_frame",
  "description": "Trigger a RenderDoc capture on the specified device and return the .rdc file path"
},
{
  "name": "adb_pull_file",
  "description": "Pull files from the device (captures, logs, etc.)"
},
{
  "name": "adb_logcat",
  "description": "Get device logcat output, filterable by tag (e.g., Vulkan validation layer logs)"
},
{
  "name": "adb_gpu_info",
  "description": "Get the device's GPU model, driver version, and supported extension list"
}
```

### Typical Flow (Cross-Device Bright-Flash Investigation)

```
1. [ADB MCP] adb_list_devices()
   → Device A: Adreno 640 (known bright flashes)
   → Device B: Mali-G78 (normal)

2. [ADB MCP] adb_capture_frame(device="A") → frame_adreno.rdc
   [ADB MCP] adb_capture_frame(device="B") → frame_mali.rdc

3. [RenderDoc MCP] load_capture("frame_adreno.rdc")
   [RenderDoc MCP] diagnose_negative_values()
   → SceneColor has 47 negative-value pixels on Adreno

4. [RenderDoc MCP] load_capture("frame_mali.rdc")
   [RenderDoc MCP] diagnose_negative_values()
   → SceneColor has 0 negative-value pixels on Mali

5. [Claude Code reasoning]
   → "Negative values appear only on Adreno, so this is not a shader logic error.
      Adreno's mediump precision causes SH sampling to produce negative values at edge normal directions.
      Mali's mediump has higher precision, so it doesn't trigger."
   → "Fix: add a max(0, x) clamp after SH sampling (on both platforms, as defensive coding)"

6. [Filesystem] Edit the shader
7. [ADB MCP] Deploy to device A to verify
```

---

## P2 Performance Log MCP: Adding Cross-Frame and CPU Views

### What It Solves

RenderDoc sees only one frame.
But some performance problems span frames:

- TAA negative-value accumulation shows its spread only over multiple frames
- Frame rate fluctuation may come from Game Thread stalls on the CPU side
- Progressive slowdown caused by GPU thermal throttling

### Key Tools

```json
{
  "name": "read_stat_log",
  "description": "Read UE stat log files (output of stat unit / stat gpu)"
},
{
  "name": "read_csv_profile",
  "description": "Read CSV-format performance profiling data (such as UE's CSV profiler output)"
},
{
  "name": "analyze_frame_time_series",
  "description": "Analyze frame time series data to detect hitch spikes, progressive slowdown, and periodic fluctuation"
}
```

---

## P2 Knowledge Base MCP: GPU Hardware Knowledge Base

### What It Solves

RenderDoc MCP's `get_capture_info` can return the GPU model.
But Claude Code needs to know what known problems that GPU has.
For example:

- On Adreno 5xx series, `texelFetch` returns wrong values with certain buffer formats
- On Mali T8xx, `imageStore` has undefined behavior on R11G11B10_FLOAT
- On PowerVR, framebuffer fetch performance drops severely in MRT mode

### Implementation

A standalone MCP is not necessarily required.
It can be integrated into Claude Code's context as a **knowledge file**:

```
/project/.claude/gpu-known-issues.md

# Adreno Known Issues
## Adreno 640 (Vulkan)
- mediump float precision: actually the FP16 minimum requirement; SH sampling edge cases may produce negative values
- R11G11B10_FLOAT: negative values clamp to 0 (not undefined behavior, but different from desktop saturate)
- ...

# Mali Known Issues
## Mali-G78
- ...
```

A better option is to embed it directly in the `known_gpu_quirks` field of `get_capture_info`.
This needs no extra tool.

---

## Strategies for Maximizing RenderDoc MCP Debugging

Besides pairing it with other MCPs, a few strategies can significantly improve RenderDoc MCP's own effectiveness.

### Strategy 1: Improve Capture Quality

Capture quality directly determines how much information RenderDoc MCP can extract.

```
Capture configuration recommendations:
───────────────────────────────────────────────────────────
✅ Keep debug info when compiling shaders (-g or -Od)
   → debug_shader_at_pixel gets exact line number and variable name mappings
   → get_shader_source returns code closer to the original source

✅ Capture with the Vulkan Validation Layer enabled
   → Some validation information is included in the capture

✅ Add debug markers / labels to draw calls and passes
   → Names returned by list_events are more readable ("SM_Rock_01" is far more useful than "DrawIndexed(3456)")
   → UE adds some markers by default, but custom ones are better

✅ Capture one "problem frame" and one "normal frame"
   → compare_pipeline_state can compare across frames
   → If the bug is intermittent, wait until it reproduces before capturing

❌ Don't capture in a Release configuration for shader debugging
   → Release shader optimization changes code structure and loses debug info
   → Capture with a Development or Debug configuration
```

### Strategy 2: Build Standardized Diagnostic Prompts

Preset diagnostic prompts for common bug types let Claude Code run the full diagnostic chain automatically:

```markdown
## Standard Diagnostic Flow Prompts

### Bright-Flash Investigation
"Load {rdc_path} and run the following diagnostics:
1. get_capture_info to confirm the device
2. diagnose_negative_values to scan the whole frame
3. For each RT with detected negative values, use sample_pixel_region to locate the bright-flash region
4. For the pixels with the worst bright flashes, run pixel_history and debug_shader_at_pixel
5. diagnose_precision_issues to check format limits
6. Combine the above to give the root cause and a fix"

### Reflection Problems
"Load {rdc_path} and run the following diagnostics:
1. diagnose_reflection_mismatch for an initial comparison
2. For the objects with the largest differences, compare state with compare_pipeline_state
3. get_shader_source to compare code differences between the two variants
4. If the difference is in constant values, compare with get_shader_constants
5. Give a concrete fix and an estimate of the brightness impact"

### Performance Optimization
"Load {rdc_path} and run the following diagnostics:
1. get_capture_info to confirm the device
2. get_pass_timing to find the most expensive pass
3. analyze_overdraw to check fill rate
4. analyze_bandwidth to check bandwidth
5. analyze_state_changes to check batching opportunities
6. diagnose_mobile_risks(categories=['performance'])
7. Give optimization suggestions sorted by ROI (optimization gain / implementation difficulty)"
```

### Strategy 3: Build a Comparative Analysis Workflow

Much rendering bug diagnosis is essentially **comparison**:

```
Comparison dimension  Purpose                                RenderDoc MCP support
─────────────────────────────────────────────────────────────────────────────────────
Within a frame        Normal DC vs reflection DC             compare_pipeline_state
                                                             diagnose_reflection_mismatch

Across frames         Normal frame vs bright-flash frame     load_capture to switch between two .rdc files
                                                             then analyze with the same tools

Across devices        Adreno frame vs Mali frame             Same as above (capture via ADB MCP)

Across versions       Before vs after a change               Use Git MCP to locate code changes

Across passes         BasePass output vs PostProcess output  pixel_history traces the full chain
```

Make it a habit in the capture flow: **save a "known good" reference frame with every capture**.
Then Claude Code can compare across frames during diagnosis.

### Strategy 4: Make the Smart Diagnostic Tools Extensible

The 4 tools in Module 8 (smart diagnostics) cover the scenarios you mentioned.
But you will certainly run into new bug types in practice.
Design a **generic diagnostic script framework**:

```json
{
  "name": "run_custom_diagnostic",
  "description": "Run a custom diagnostic script. The script can call all the underlying tools. Used to add new diagnostic logic quickly without modifying the MCP Server code.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "script_path": {
        "type": "string",
        "description": "Path to a Python diagnostic script, which can call all of the RenderDoc ReplayController APIs"
      },
      "params": {
        "type": "object",
        "description": "Parameters passed to the script"
      }
    },
    "required": ["script_path"]
  }
}
```

This helps when you hit a new bug type.
For example, one day you need to diagnose "wrong SSBO output from a compute shader".
Claude Code can write and run a diagnostic script directly.
It does not need to wait for an MCP Server update.

---

## Recommended Rollout Path

```
Phase 1 (1-2 weeks): Minimum viable
────────────────────────
RenderDoc MCP P0 tools (9 basic tools)
+ Claude Code's built-in Filesystem capability
→ Can do: basic frame browsing, shader viewing, pixel diagnosis, simple fixes

Phase 2 (2-4 weeks): Core scenario coverage
────────────────────────
RenderDoc MCP P1 tools (+6 high-value tools)
+ Git integration (Claude Code uses the git CLI directly)
→ Can do: bright-flash diagnosis, negative-value tracing, reflection comparison, performance analysis, code fix + commit

Phase 3 (1-2 months): Complete system
────────────────────────
RenderDoc MCP P2 tools (+7 enhancement tools)
+ UE Editor MCP (if engine-level queries are needed)
+ ADB integration (for multi-device testing)
→ Can do: fully automated end-to-end diagnosis, cross-device comparison, mobile risk pre-checks

Phase 4 (ongoing iteration): Intelligence
────────────────────────
Accumulate diagnostic cases → distill them into standardized prompts and custom diagnostic scripts
Keep expanding the GPU known-issues knowledge base
Add new smart diagnostic tools based on pain points in real use
```

---

## Full Toolchains by Scenario

### Scenario: Dark Reflection (Full Loop)

```
[RenderDoc MCP]  diagnose_reflection_mismatch       → Locate the difference
[RenderDoc MCP]  debug_shader_at_pixel              → Confirm the shader-level cause
[Filesystem]     Read MobileBasePassPixelShader.usf → Find the source
[Git]            git blame on that file             → See who recently changed the reflection branch
[Filesystem]     Edit the shader code               → Fix
[Git]            Commit to a fix branch             → Version management
[ADB] (optional) Capture on the target device       → Confirm the fix works
```

### Scenario: TAA Bright Flashes (Full Loop)

```
[ADB] (optional) Capture on the bright-flash device  → Get the .rdc
[RenderDoc MCP]  get_capture_info                    → Confirm the GPU model
[RenderDoc MCP]  diagnose_negative_values            → Locate the negative-value source and TAA amplification
[RenderDoc MCP]  debug_shader_at_pixel               → Trace the TAA shader execution
[RenderDoc MCP]  diagnose_precision_issues           → Confirm the R11G11B10 format limit
[Filesystem]     Read the TAA shader and IBL shader  → Find the code to change
[Filesystem]     Edit: IBL clamp + TAA history clamp → Two-part fix
[Git]            Commit the fix                      → Version management
[ADB] (optional) Multi-device verification           → Confirm all GPUs are fine
```

### Scenario: Performance Optimization (Full Loop)

```
[RenderDoc MCP]  get_pass_timing                         → Find the bottleneck pass
[RenderDoc MCP]  analyze_overdraw                        → Find fill rate hotspots
[RenderDoc MCP]  analyze_bandwidth                       → Find bandwidth bottlenecks
[RenderDoc MCP]  analyze_state_changes                   → Find batching opportunities
[RenderDoc MCP]  diagnose_mobile_risks                   → Overall risk assessment
[Filesystem]     Edit rendering code / project config    → Apply the optimization
[Git]            Commit the optimization                 → Version management
[RenderDoc MCP]  Load the optimized frame and re-analyze → Verify the optimization
```

---

## Summary

**RenderDoc MCP alone is a powerful diagnostic engine** that covers most frame-level analysis needs.
But to close the loop from "finding the problem" to "fixing the problem":

- **Required companions**: Filesystem (read/write code) + Git (version management).
  Claude Code already has both built in.
- **Strongly recommended**: UE Editor MCP (understanding the engine-level why) + ADB (multi-device support)
- **Nice to have**: Performance Log (cross-frame analysis) + knowledge base (GPU hardware quirks)

Getting the most out of RenderDoc MCP depends less on how many tools you pair it with than on:
1. **Capture quality**: shader debug info + debug markers + saved reference frames
2. **Comparative thinking**: within-frame, cross-frame, cross-device, and cross-version comparison
3. **Distilling diagnostics**: turn the experience from each investigation into standardized prompts and reusable diagnostic scripts
