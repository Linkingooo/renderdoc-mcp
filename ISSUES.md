# renderdoc-mcp Issue List

Issues found in a full code review, sorted by priority.

## P0: Must Fix

### 1. `find_draws` corrupts session event state
- **File**: `event_tools.py:224`
- **Problem**: Calls `controller.SetFrameEvent()` directly.
  This bypasses `session.set_event()`, so `_current_event` goes out of sync.
- **Fix**: Restore the original event state when done.
- **Status**: ✅ Fixed

### 2. `save_texture`/`save_render_target` call `os.makedirs` on an empty path
- **File**: `data_tools.py:56, 338`
- **Problem**: `os.path.dirname("file.png")` returns `""`.
  `os.makedirs("")` raises an exception.
- **Fix**: Add an `or "."` guard.
  `export_mesh` already has it.
- **Status**: ✅ Fixed

### 3. `serialize_shader_variable` always reads float32
- **File**: `util.py:306`
- **Problem**: Always uses `var.value.f32v`.
  Integer/bool uniform values are misread.
- **Fix**: Pick the right accessor based on `var.type`.
- **Status**: ✅ Fixed

## P1: Important Improvements

### 4. `get_cbuffer_contents`/`disassemble_shader` do not support the compute stage
- **File**: `shader_tools.py:71, 234`
- **Problem**: Unconditionally calls `GetGraphicsPipelineObject()`.
  This fails for compute.
- **Fix**: Pick the Graphics or Compute PipelineObject based on the stage.
- **Status**: ✅ Fixed

### 5. `except Exception: pass` silently swallows exceptions → add warnings
- **File**: `pipeline_tools.py` (10+ places)
- **Problem**: When a pipeline query fails, the JSON is missing keys.
  Users cannot tell "unsupported" from "error".
- **Fix**: Add a `warnings` list to the result.
- **Status**: ✅ Fixed

### 6. `pixel_history` lacks failure reasons
- **File**: `advanced_tools.py:54-56`
- **Problem**: Only reports `Passed()`.
  It does not report depthTestFailed/stencilTestFailed/backfaceCulled.
- **Fix**: Read the detailed failure fields of PixelModification.
- **Status**: ✅ Fixed

### 7. Inconsistent `event_id` parameter handling
- **File**: `data_tools.py` (save_texture, pixel_history)
- **Problem**: Some similar tools accept event_id and others do not.
- **Fix**: Add an optional event_id parameter to `save_texture` and `pixel_history`.
- **Status**: ✅ Fixed

## P2: Quality Improvements

### 8. `export_mesh` assumes TriangleList topology
- **File**: `data_tools.py:488-497`
- **Problem**: Face generation always steps 3 vertices at a time.
  Strip/Fan topologies produce a wrong OBJ.
- **Fix**: Check the topology and generate faces accordingly.
- **Status**: ✅ Fixed

### 9. `search_actions`/`list_resources` do not handle invalid regexes
- **File**: `event_tools.py:156`, `resource_tools.py:83`
- **Problem**: `re.compile(user_pattern)` raises `re.error` with no try/except.
- **Fix**: Catch re.error and return a friendly error message.
- **Status**: ✅ Fixed

### 10. Enum values shown as raw numbers
- **File**: `util.py`, `resource_tools.py`, `shader_tools.py`
- **Problem**: usage/var_type/system_value/texture dimension and others show "0", "32".
  They should show readable names.
- **Fix**: Add enum mapping tables.
- **Status**: ✅ Fixed

### 11. `get_cbuffer_contents` out-of-range error message is misleading
- **File**: `shader_tools.py:229`
- **Problem**: Shows `"out of range (0--1)"` when the shader has no cbuffer.
- **Fix**: Special-case len=0 and output "shader has no constant buffers".
- **Status**: ✅ Fixed

## P3: Code Cleanup

### 12. Clean up dead code and encapsulation leaks
- **File**: `util.py`, `session.py`, several tool modules
- **Problem**: `result_or_raise` is unused.
  Tools access `session._action_map`/`session._cap` directly.
- **Fix**: Delete the dead code and add public accessor methods on session.
- **Status**: ✅ Fixed
