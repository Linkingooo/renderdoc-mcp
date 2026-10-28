# RenderDoc MCP Server Automated Evaluation Guide

> This document provides a structured evaluation framework.
> It lets an LLM systematically verify the quality of a RenderDoc MCP Server.
> The evaluator (an LLM) should run the test cases in this document step by step.
> Record the result of each step.
> Produce an evaluation report at the end.

---

## 1. Pre-Evaluation Setup

### 1.1 Environment Check

Before starting the evaluation, run these checks:

1. Confirm the MCP Server is running and reachable
2. Call `tools/list` to get the full list of available tools
3. Call `resources/list` to get the full list of available resources (if supported)
4. Call `prompts/list` to get the full list of available prompt templates (if supported)
5. Record the Server's name, version, and declared capabilities

### 1.2 Evaluation Output Template

After each test phase, record the result in this format:

```
[Phase name]
- Test item: <what was tested>
- Result: ✅ Pass / ⚠️ Partial pass / ❌ Fail
- Details: <observed behavior>
- Suggestion: <improvement suggestion (if any)>
```

---

## 2. Tool Definition Quality (Weight 25%)

This phase does not require calling any tools.
Review only the tool metadata: name, description, and parameter schema.

### 2.1 Naming Convention Check

Check each tool name and answer these questions:

- Does the name clearly express the tool's function?
  For example, `capture_frame` is better than `doAction`.
- Do the names follow a consistent style?
  Use all snake_case or all camelCase, not a mix.
- Are any names vague or too broad?
  Examples: `get_data`, `process`, `run`.
- Are there tools with similar names but different functions that could cause confusion?

**Scoring criteria**:
- ✅ All tool names are clear, consistent, and unambiguous
- ⚠️ Most tool names are good, a few are vague
- ❌ Naming is chaotic, and several tools are hard to tell apart

### 2.2 Description Completeness Check

Analyze the `description` field of each tool:

- Does it explain the tool's purpose and use cases?
- Does it explain when to use this tool (rather than another)?
- Does it state preconditions?
  For example, "call `load_capture` first to load the .rdc file".
- Does it explain the meaning and format of the return value?
- If it uses RenderDoc terminology, does it give enough context?

**Test method**: Using only the tool descriptions, try to answer the questions below.
If you can't, the descriptions are incomplete:

1. "I want to see the input textures of a draw call" → which tool should I use?
2. "I want to compare the performance of two frames" → which tool should I use?
3. "I want to find the draw calls causing overdraw" → which tool should I use?

### 2.3 Parameter Schema Check

Review the `inputSchema` of each tool:

- Does every parameter have an explicit `type`?
- Are `required` and optional parameters distinguished?
- Do enum types list all valid values?
  Examples: shader stage, texture format.
- Does each parameter's `description` explain the expected input?
- Are there parameters that should have a default value but don't?
- Do numeric parameters have reasonable range constraints (`minimum`/`maximum`)?

**Pay special attention to**:
- `event_id` / `draw_call_id` parameters: is it explained how to obtain a valid ID?
- File path parameters: is the path format (absolute/relative) specified?
- Coordinate/pixel parameters: are the coordinate origin and range specified?

---

## 3. Functional Correctness (Weight 25%)

This phase requires actually calling the tools to verify they work.
Prepare a RenderDoc capture file (.rdc) first.

### 3.1 Basic Workflow Tests

Run the operations in this order to simulate the most common usage:

**Test A: Load and browse**

```
Step 1: Load the .rdc capture file
Step 2: Get the list of all draw calls / events in the frame
Step 3: Select a specific draw call
Step 4: Get the details of that draw call
```

Check at each step:
- Does the call return successfully?
- Is the returned data complete and correctly formatted?
- Can the returned IDs/references be used in later steps?

**Test B: Shader analysis**

```
Step 1: Get the shader information of a draw call
Step 2: View the source or decompiled output of the vertex shader
Step 3: View the source or decompiled output of the pixel/fragment shader
Step 4: Get the shader's input/output binding information
```

**Test C: Texture and resource inspection**

```
Step 1: List all textures bound to a draw call
Step 2: Get a texture's metadata (format, resolution, mip levels)
Step 3: Export/view a texture's contents (if supported)
Step 4: View the state of the render target / framebuffer
```

**Test D: Pipeline state queries**

```
Step 1: Get the full pipeline state of a draw call
Step 2: View the blend state settings
Step 3: View the depth/stencil state settings
Step 4: View the rasterizer state (culling, polygon mode, etc.)
```

### 3.2 Edge Case Tests

These tests verify tool behavior on abnormal input:

| Scenario | Input | Expected behavior |
|---------|------|---------|
| Invalid event ID | `event_id: 999999` | Returns a clear "event not found" error |
| Query with no capture loaded | Query a draw call before calling `load_capture` | Returns a "no capture loaded" error |
| Empty parameters | Missing a required parameter | Returns a validation error naming the missing parameter |
| Wrong type | `event_id: "abc"` | Returns a type validation error |
| Out-of-range pixel coordinates | `x: -1, y: -1` or beyond the texture size | Returns a range error |
| Unsupported file format | Load a non-.rdc file | Returns an unsupported-format error |

**Scoring criteria**:
- ✅ All edge cases return clear, actionable error messages
- ⚠️ Some error messages are vague or lack detail
- ❌ Crashes, timeouts, or empty content returned instead of an error message

### 3.3 Error Message Quality

Evaluate all the error messages collected in the previous step:

- Does the message clearly identify the problem?
- Does it suggest how to fix it?
  For example, "event_id 999999 does not exist. The event range of the current frame is 0-1523."
- Does it use natural language an LLM can understand and reason about, rather than cryptic error codes?
- Does the error response use the standard MCP `isError: true` flag?

---

## 4. LLM Interaction Quality (Weight 20%)

This phase evaluates how friendly the tools are to an LLM as the user.

### 4.1 Return Value Readability

Analyze the return value of each tool:

- Is the return value clearly structured?
  It should have a clear hierarchy, with key information standing out.
- Do numeric values have units?
  For example, ms or μs for time, pixels or bytes for size.
- Do enum values use readable strings rather than bare numbers?
  For example, `"TRIANGLES"` rather than `4`.
- Is the amount of returned data moderate?
  It should neither drop key information nor flood the context window.

**Special test**: Get the full information of a complex draw call and check the token count of the response.
If a single response exceeds 2000 tokens, consider whether there is information overload.

### 4.2 Tool Chain Naturalness

Verify that an LLM can derive the following workflows naturally from the tool descriptions alone:

**Scenario 1: Performance analysis**
> "Find the 5 slowest draw calls in the frame and analyze their shader complexity and texture usage"

Try to plan the execution steps using only the tool list and descriptions.
Evaluate:
- Do you need to jump back and forth between unrelated tools?
- Are there "information gaps" between steps?
  That is, the output of one tool can't be used directly as input to the next.

**Scenario 2: Rendering bug investigation**
> "An object appears black on screen; help me find the cause"

A reasonable investigation path might include:
1. Find the draw call that renders the object
2. Check whether the textures are bound correctly
3. Check the shader for compile errors
4. Check whether the blend/depth state makes it invisible
5. View the render target output

Evaluate whether the toolset fully covers this path.

**Scenario 3: Mobile performance optimization (relevant to your daily work)**
> "Analyze overdraw in the frame and find draw calls that can be batched"

Evaluate:
- Is there a tool that provides overdraw data?
- Is there a tool that analyzes the state-switch cost between draw calls?
- Can you get enough information to judge whether batching is feasible?
  That means checking for the same material, shader, and render state.

### 4.3 Tool Granularity

- Are there "super tools" that do too much?
  For example, one tool takes 10+ parameters and behaves completely differently depending on them.
- Is anything split too finely?
  For example, viewing a draw call's basic information takes 5+ tool calls.
- Do tools at the same level overlap in function?

**Ideal granularity**: A typical RenderDoc analysis task should take 3-8 tool calls.

---

## 5. Engineering Quality (Weight 15%)

### 5.1 Performance Tests

Record the response time of these operations:

| Operation | Acceptable time | Actual time | Result |
|-----|-----------|---------|------|
| Load an .rdc file | < 10s | | |
| Get the event list | < 2s | | |
| Get single draw call details | < 1s | | |
| Get shader source | < 2s | | |
| Get texture metadata | < 1s | | |
| Export a texture image | < 5s | | |

### 5.2 Stability Tests

- Call the same tool 10 times in a row.
  Are the results consistent?
- Call different tools in rapid succession to simulate continuous LLM reasoning.
  Do timeouts or errors occur?
- After loading a large capture file (> 100MB), do the tools still respond normally?

### 5.3 Security Checks

- Do file path parameters have directory traversal protection?
  For example, try passing `../../etc/passwd`.
- Is the server read-only?
  An MCP Server should not be able to modify or delete local files unless explicitly required.
- Is the set of accessible file types restricted?

---

## 6. Coverage (Weight 15%)

Check the MCP Server's coverage against RenderDoc's core features:

### 6.1 Feature Coverage Matrix

| RenderDoc core feature | Has a corresponding tool | Tool name | Completeness |
|-------------------|:------------:|---------|-------|
| Load / open capture files | ☐ | | |
| Browse Event / Draw Call list | ☐ | | |
| View Pipeline State | ☐ | | |
| View Shaders (source/decompiled) | ☐ | | |
| View bound textures and Buffers | ☐ | | |
| View Mesh / vertex data | ☐ | | |
| View Render Target output | ☐ | | |
| Pixel-level debugging (Pixel History) | ☐ | | |
| Shader debugging / single-stepping | ☐ | | |
| Resource usage statistics | ☐ | | |
| API call tracing | ☐ | | |
| Performance timing data | ☐ | | |
| Texture export | ☐ | | |
| Capture file comparison | ☐ | | |

### 6.2 Critical Gap Analysis

List the features that are essential for typical RenderDoc use cases but currently missing.
Typical use cases are performance analysis, bug investigation, and shader debugging.
Sort them by priority:

```
P0 (blocks core workflows):            <list of missing features>
P1 (significantly reduces efficiency): <list of missing features>
P2 (nice to have):                     <list of missing features>
```

---

## 7. Overall Evaluation Report

After all tests are complete, produce an overall evaluation report in this format:

```markdown
# RenderDoc MCP Server Evaluation Report

## Basic Information
- Server name: 
- Version: 
- Tool count: 
- Evaluation date: 

## Total Score: __/100

## Scores by Dimension

| Dimension | Weight | Score (0-100) | Weighted score |
|---------|------|-----------|---------|
| Tool definition quality | 25% | | |
| Functional correctness | 25% | | |
| LLM interaction quality | 20% | | |
| Engineering quality | 15% | | |
| Feature coverage | 15% | | |

## Key Findings

### Strengths
1. 
2. 
3. 

### Areas for Improvement
1. 
2. 
3. 

### Blocking Issues (if any)
1. 

## Detailed Test Records
(Attach the results of each phase)

## Improvement Priorities

### Fix Immediately
- 

### Short Term (1-2 Weeks)
- 

### Long Term
- 
```

---

## 8. Usage Notes

### Instructions for the Evaluator (LLM)

1. **Run in order**: Go through the chapters starting from chapter 2.
   Don't skip any test item.
2. **Record honestly**: Record even small problems.
   Don't ignore details because things are "generally fine".
3. **Think from the user's perspective**: You are both the evaluator and a user of this tool.
   If a tool confuses you, that is a problem.
4. **Distinguish severity**: Not all problems are equally important.
   Mark which are blocking and which are advisory.
5. **Give specific suggestions**: Don't just say "the description is unclear".
   Say how it should be rewritten.

### Custom Extensions

This evaluation framework can be extended as the project requires:

- Add dedicated tests for a specific graphics API, such as Vulkan, DirectX, OpenGL, or Metal.
- Add checks specific to mobile rendering for mobile platforms, such as your FPS mobile game project.
- Add targeted tests for the rendering pipeline of a specific engine, such as UE 4.26.
- Integrate automation scripts to run some tests in batch
