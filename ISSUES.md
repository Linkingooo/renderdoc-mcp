# renderdoc-mcp 问题清单

全面代码审查发现的问题，按优先级排序。

## P0 — 必须修复

### 1. `find_draws` 破坏 session 事件状态
- **文件**: `event_tools.py:224`
- **问题**: 直接调用 `controller.SetFrameEvent()` 绕过 `session.set_event()`，`_current_event` 不同步
- **修复**: 完成后恢复原始事件状态
- **状态**: ✅ 已修复

### 2. `save_texture`/`save_render_target` 的 `os.makedirs` 空路径
- **文件**: `data_tools.py:56, 338`
- **问题**: `os.path.dirname("file.png")` 返回 `""`，`os.makedirs("")` 抛异常
- **修复**: 加 `or "."` 保护（`export_mesh` 已有此处理）
- **状态**: ✅ 已修复

### 3. `serialize_shader_variable` 固定读 float32
- **文件**: `util.py:306`
- **问题**: 总是用 `var.value.f32v`，整数/布尔 uniform 值被错误解读
- **修复**: 根据 `var.type` 选择正确的 accessor
- **状态**: ✅ 已修复

## P1 — 重要改进

### 4. `get_cbuffer_contents`/`disassemble_shader` 不支持 compute stage
- **文件**: `shader_tools.py:71, 234`
- **问题**: 无条件调用 `GetGraphicsPipelineObject()`，compute 会失败
- **修复**: 根据 stage 选择 Graphics/Compute PipelineObject
- **状态**: ✅ 已修复

### 5. `except Exception: pass` 静默吞异常 → 加 warnings
- **文件**: `pipeline_tools.py` (10+处)
- **问题**: 管线查询失败时 JSON 缺少 key，用户无法区分"不支持"还是"出错"
- **修复**: 在返回结果中加 `warnings` 列表
- **状态**: ✅ 已修复

### 6. `pixel_history` 缺少失败原因
- **文件**: `advanced_tools.py:54-56`
- **问题**: 只报 `Passed()`，不报 depthTestFailed/stencilTestFailed/backfaceCulled
- **修复**: 读取 PixelModification 的详细失败字段
- **状态**: ✅ 已修复

### 7. `event_id` 参数模式不一致
- **文件**: `data_tools.py` (save_texture, pixel_history)
- **问题**: 功能相似的工具有的接受 event_id 有的不接受
- **修复**: 给 `save_texture` 和 `pixel_history` 加 optional event_id 参数
- **状态**: ✅ 已修复

## P2 — 质量改善

### 8. `export_mesh` 假设 TriangleList 拓扑
- **文件**: `data_tools.py:488-497`
- **问题**: 面生成固定按每 3 顶点步进，Strip/Fan 拓扑输出错误 OBJ
- **修复**: 检查 topology 并正确生成面
- **状态**: ✅ 已修复

### 9. `search_actions`/`list_resources` 未处理非法正则
- **文件**: `event_tools.py:156`, `resource_tools.py:83`
- **问题**: `re.compile(user_pattern)` 抛 `re.error` 无 try/except
- **修复**: 捕获 re.error 返回友好错误信息
- **状态**: ✅ 已修复

### 10. 枚举值显示为原始数字
- **文件**: `util.py`, `resource_tools.py`, `shader_tools.py`
- **问题**: usage/var_type/system_value/texture dimension 等显示 "0", "32" 而非可读名
- **修复**: 增加枚举映射表
- **状态**: ✅ 已修复

### 11. `get_cbuffer_contents` 越界错误信息误导
- **文件**: `shader_tools.py:229`
- **问题**: shader 无 cbuffer 时显示 `"out of range (0--1)"`
- **修复**: 特判 len=0 输出 "shader has no constant buffers"
- **状态**: ✅ 已修复

## P3 — 代码清理

### 12. 清理死代码和封装泄露
- **文件**: `util.py`, `session.py`, 多个工具模块
- **问题**: `result_or_raise` 未使用；工具直接访问 `session._action_map`/`session._cap`
- **修复**: 删除死代码，在 session 上加公共访问方法
- **状态**: ✅ 已修复

## P0 — 新发现（针对当前 RenderDoc 2026 API）

### 13. `PipeState` 方法名过时 → 管线状态全部丢失
- **文件**: `pipeline_tools.py` (4 处), `event_tools.py` (1 处), `performance_tools.py` (3 处), `diagnostic_tools.py` (2 处)
- **问题**: 使用旧 API 名，当前 RenderDoc 中不存在，全部抛 `AttributeError` 被吞进 `warnings`：
  - `GetColorBlend()` → 现为 `GetColorBlends()`（直接返回 `List[ColorBlend]`）；混合因子用 `GetBlendFactor()`
  - `GetDepthState()` → 现为 `GetDepthTestState()`
  - `GetStencilState()` → 现为 `IsStencilTestEnabled()`（`stencilEnable` 已移除）
  - `GetRasterizer()` → 现为 `GetRasterState()`（仅 `frontCCW`/`fillMode`/`cullMode`，无 `depthBias`/`depthClip`/`scissorEnable`/`multisampleEnable`）
  - `GetViewports()`/`GetScissors()` → 现为 `GetViewport(i)`/`GetScissor(i)`（需按索引枚举）
- **影响**: `get_draw_call_state`/`get_pipeline_state` 丢失 blend/depth/stencil/rasterizer/viewport；`find_draws(blend=)`、`analyze_state_changes`、`diagnose_reflection_mismatch` 的 blend/depth/cull 对比失效
- **修复**: 改用当前 API 名；viewport/scissor 按索引 0..15 枚举、遇空值停止
- **验证**: 4 个 D3D11 capture（draw_zoo/texture_zoo/mesh_zoo/cbuffer_zoo）实测全部工具无 warnings
- **状态**: ✅ 已修复（需重启 MCP 服务生效）
