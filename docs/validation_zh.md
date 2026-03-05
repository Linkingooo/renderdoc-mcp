# MCP 数据验证工具文档

## 背景

MCP 工具从 RenderDoc replay API 读取 GPU 帧数据时，可能因为以下原因返回错误数据：

- **Replay 不稳定**：同一事件多次回放结果不一致（驱动 bug、GPU 状态残留）
- **事件导航错误**：SetFrameEvent 没有真正切换到目标事件，读到了其他事件的数据
- **类型解析错误**：float/int/half 类型判断错误，导致数值被错误解释
- **Resource ID 错误**：查询了错误的资源，返回不相关的数据
- **缓存过期**：session 缓存的 resource ID 或 texture 描述与实际不一致

验证工具通过**两条独立的 API 路径**读取同一数据并对比，来确认数据是否可信。

## 验证工具一览

| 工具 | 用途 | 交叉验证方法 |
|------|------|-------------|
| `validate_capture_integrity` | 打开 capture 后的全面检查 | 缓存 vs 实时查询、事件导航、帧边界 |
| `validate_event_data` | 单个 event 的数据完整性 | pipeline state + resource ID + RT 像素采样 |
| `cross_validate_pixel` | 像素值交叉验证 | **PickPixel vs GetTextureData（原始字节解析）** |
| `validate_cbuffer` | 常量缓冲区交叉验证 | **GetCBufferVariableContents vs GetBufferData（原始字节解析）** |
| `validate_vertex_output` | 顶点数据交叉验证 | **双读对比 + NaN/Inf/退化顶点检测** |
| `validate_resource` | 资源 ID 有效性 | 缓存 vs GetResources() 交叉比对 |

## 核心原理：用不同 API 路径读同一数据

### 像素值验证 (`cross_validate_pixel`)

```
路径 A: PickPixel(tex_id, x, y)
        ↓ GPU 采样器读取单个像素
        ↓ 返回 floatValue[4]

路径 B: GetTextureData(tex_id, subresource)
        ↓ 读取纹理的完整原始字节
        ↓ 根据格式（R8G8B8A8_UNORM / R16G16B16A16_FLOAT / R32G32B32A32_FLOAT 等）
        ↓ 手动定位 (y * width + x) * bpp 偏移
        ↓ 解析出 RGBA 值
```

两条路径使用完全不同的 RenderDoc 内部代码路径。如果结果一致，数据可信。

**支持的纹理格式（原始字节解析）：**
- `R32G32B32A32_FLOAT` — 16 字节/像素
- `R32G32B32_FLOAT` — 12 字节/像素
- `R16G16B16A16_FLOAT` — 8 字节/像素（半精度浮点）
- `R8G8B8A8_UNORM` — 4 字节/像素
- `B8G8R8A8_UNORM` — 4 字节/像素（BGRA 字节序）
- `R11G11B10_FLOAT` — 4 字节/像素（压缩浮点）
- `R32_FLOAT` — 4 字节/像素（单通道）
- `R16G16_FLOAT` — 4 字节/像素

不支持的格式会自动回退到 double-PickPixel 方式。

### 常量缓冲区验证 (`validate_cbuffer`)

```
路径 A: GetCBufferVariableContents(pipe, shader, stage, entry, index, resource, ...)
        ↓ RenderDoc 结构化解析
        ↓ 返回带类型信息的变量树（名称、类型、值）

路径 B: GetBufferData(cbuffer_resource, offset, size)
        ↓ 读取 cbuffer 的原始字节
        ↓ 按 float32 解释为平坦数组
        ↓ 按变量声明顺序与路径 A 的值逐一对比
```

如果结构化解析和原始字节解析的值不一致，说明类型解释可能有误。

### 顶点数据验证 (`validate_vertex_output`)

```
路径 A: GetPostVSData(0, 0, VSOut) → 获取 buffer ID
        ↓ GetBufferData(vertexResourceId, offset, size)
        ↓ 解析为 float 数组

路径 B: 重新导航到同一事件（SetFrameEvent）
        ↓ 再次 GetPostVSData + GetBufferData
        ↓ 对比前 10 个顶点的值
```

额外检查：
- NaN / Inf 检测
- 退化顶点 (0,0,0) 检测
- W 分量异常（W=0）检测
- `action.numIndices` 与 `PostVS.numIndices` 一致性
- 顶点 stride 一致性

## 使用方式

### 1. 打开 capture 后立即检查

```
validate_capture_integrity()
```

确认 capture 文件和 replay 环境是可靠的。检查内容：
- action map 中的事件能否正确导航
- 资源缓存是否与实时查询一致
- 所有纹理的尺寸是否合理
- 帧首尾事件能否正常访问

### 2. 分析关键 draw call 前验证

```
validate_event_data(event_id=1234)
```

确认该事件的数据环境正确：pipeline state、shader 绑定、resource ID、RT 像素。

### 3. 怀疑像素值错误时

```
cross_validate_pixel(resource_id="ResourceId::4749", x=512, y=384, event_id=1234)
```

用两种 API 路径读同一像素，返回：
- `pick_rgba` — PickPixel 的结果
- `raw_rgba` — GetTextureData 原始字节解析的结果
- `consistent` — 是否一致
- `mismatches` — 不一致的通道和差值

### 4. 怀疑 cbuffer 值错误时

```
validate_cbuffer(stage="pixel", cbuffer_index=0, event_id=1234)
```

返回：
- `cross_validation` — 结构化 vs 原始字节的对比结果
- `value_integrity` — NaN/Inf/极大值检测
- `binding_resource` — cbuffer 绑定的 resource ID 有效性

### 5. 怀疑顶点数据错误时

```
validate_vertex_output(event_id=1234, stage="vsout")
```

返回：
- `double_read_consistency` — 双读一致性
- `vertex_integrity` — 顶点数据质量检测
- `action_consistency` — numIndices 一致性

## 返回格式

所有验证工具返回统一的 JSON 格式：

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

有问题时：

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

## 验证辅助函数（validation.py）

供内部使用或自定义验证逻辑：

| 函数 | 用途 |
|------|------|
| `validate_pixel_value(rgba)` | 检测 NaN / Inf |
| `validate_resource_id(rid_str)` | 检查格式和空值 |
| `validate_event_consistency(expected, actual)` | 事件导航一致性 |
| `validate_texture_dimensions(w, h, d)` | 纹理尺寸合理性 |
| `validate_float_array(values, context)` | 浮点数组异常 |
| `cross_validate_pixel(pick, read, tolerance)` | 像素对比 |
| `validate_pipeline_state(state)` | pipeline 常见问题 |
| `validate_vertex_data(vertices, num_indices, pos_offset)` | 顶点数据质量 |
| `validate_cbuffer_variables(variables, byte_size)` | cbuffer 变量异常 |
| `cross_validate_float_arrays(arr1, arr2, tolerance)` | 浮点数组逐元素对比 |
| `build_validation_summary(checks)` | 构建统一的检查报告 |

## 测试

```bash
python -m unittest tests.test_validation -v
```

当前 70 个单元测试覆盖所有验证函数，包括：
- 半精度浮点解析 (half → float)
- R11G11B10_FLOAT 解包
- 各种纹理格式的原始字节解析
- 结构化变量 → 平坦浮点数组提取
- 边界情况：NaN、Inf、空数据、格式不支持
