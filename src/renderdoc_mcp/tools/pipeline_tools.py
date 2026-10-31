"""Pipeline inspection tools: get_pipeline_state, get_shader_bindings, get_vertex_inputs, get_draw_call_state."""

from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import MCPServer

from renderdoc_mcp.session import get_session
from renderdoc_mcp.util import (
    rd,
    to_json,
    make_error,
    SHADER_STAGE_MAP,
    BLEND_FACTOR_MAP,
    BLEND_OP_MAP,
    COMPARE_FUNC_MAP,
    STENCIL_OP_MAP,
    CULL_MODE_MAP,
    FILL_MODE_MAP,
    TOPOLOGY_MAP,
    enum_str,
    blend_formula,
    serialize_sig_element,
)


def _serialize_viewport(vp) -> dict:
    return {
        "x": vp.x, "y": vp.y,
        "width": vp.width, "height": vp.height,
        "min_depth": vp.minDepth, "max_depth": vp.maxDepth,
    }


def _serialize_scissor(sc) -> dict:
    return {
        "x": sc.x, "y": sc.y,
        "width": sc.width, "height": sc.height,
    }


def _serialize_blend_eq(eq) -> dict:
    return {
        "source": enum_str(eq.source, BLEND_FACTOR_MAP, "BlendFactor."),
        "destination": enum_str(eq.destination, BLEND_FACTOR_MAP, "BlendFactor."),
        "operation": enum_str(eq.operation, BLEND_OP_MAP, "BlendOp."),
    }


def _serialize_pipeline_state(state) -> dict:
    """Extract a comprehensive pipeline state summary."""
    result: dict = {}
    warnings: list[str] = []

    # Shader stages
    shaders = {}
    for name, stage in SHADER_STAGE_MAP.items():
        if stage == rd.ShaderStage.Compute:
            continue
        refl = state.GetShaderReflection(stage)
        if refl is not None:
            shaders[name] = {
                "bound": True,
                "entry_point": state.GetShaderEntryPoint(stage),
                "resource_id": str(refl.resourceId),
            }
        else:
            shaders[name] = {"bound": False}
    result["shaders"] = shaders

    # Input assembly
    try:
        topo = state.GetPrimitiveTopology()
        result["topology"] = enum_str(topo, TOPOLOGY_MAP, "Topology.")
    except Exception as e:
        warnings.append(f"topology: {type(e).__name__}: {e}")

    # Viewports and scissors
    try:
        viewports = state.GetViewports()
        result["viewports"] = [_serialize_viewport(vp) for vp in viewports]
    except Exception as e:
        warnings.append(f"viewports: {type(e).__name__}: {e}")

    try:
        scissors = state.GetScissors()
        result["scissors"] = [_serialize_scissor(sc) for sc in scissors]
    except Exception as e:
        warnings.append(f"scissors: {type(e).__name__}: {e}")

    # Rasterizer state
    try:
        raster = state.GetRasterizer()
        result["rasterizer"] = {
            "fill_mode": enum_str(raster.fillMode, FILL_MODE_MAP, "FillMode."),
            "cull_mode": enum_str(raster.cullMode, CULL_MODE_MAP, "CullMode."),
            "front_ccw": raster.frontCCW,
            "depth_bias": raster.depthBias,
            "depth_clip": raster.depthClip,
            "scissor_enable": raster.scissorEnable,
            "multisample_enable": raster.multisampleEnable,
        }
    except Exception as e:
        warnings.append(f"rasterizer: {type(e).__name__}: {e}")

    # Color blend
    try:
        cb = state.GetColorBlend()
        blends = []
        for b in cb.blends:
            color_eq = _serialize_blend_eq(b.colorBlend)
            alpha_eq = _serialize_blend_eq(b.alphaBlend)
            entry = {
                "enabled": b.enabled,
                "write_mask": b.writeMask,
                "color": color_eq,
                "alpha": alpha_eq,
            }
            if b.enabled:
                entry["formula"] = blend_formula(
                    color_eq["source"], color_eq["destination"], color_eq["operation"],
                    alpha_eq["source"], alpha_eq["destination"], alpha_eq["operation"],
                )
            blends.append(entry)
        result["color_blend"] = {
            "blend_factor": [cb.blendFactor.x, cb.blendFactor.y, cb.blendFactor.z, cb.blendFactor.w],
            "blends": blends,
        }
    except Exception as e:
        warnings.append(f"color_blend: {type(e).__name__}: {e}")

    # Depth state
    try:
        ds = state.GetDepthState()
        result["depth_state"] = {
            "depth_enable": ds.depthEnable,
            "depth_function": enum_str(ds.depthFunction, COMPARE_FUNC_MAP, "CompareFunc."),
            "depth_write_mask": ds.depthWrites,
        }
    except Exception as e:
        warnings.append(f"depth_state: {type(e).__name__}: {e}")

    # Stencil state
    try:
        ss = state.GetStencilState()
        result["stencil_state"] = {
            "stencil_enable": ss.stencilEnable,
        }
    except Exception as e:
        warnings.append(f"stencil_state: {type(e).__name__}: {e}")

    # Output targets
    try:
        outputs = state.GetOutputTargets()
        result["output_targets"] = [
            {"resource_id": str(o.resource)} for o in outputs if int(o.resource) != 0
        ]
    except Exception as e:
        warnings.append(f"output_targets: {type(e).__name__}: {e}")

    try:
        depth = state.GetDepthTarget()
        if int(depth.resource) != 0:
            result["depth_target"] = {"resource_id": str(depth.resource)}
    except Exception as e:
        warnings.append(f"depth_target: {type(e).__name__}: {e}")

    if warnings:
        result["warnings"] = warnings

    return result


def register(mcp: MCPServer):
    @mcp.tool()
    def get_pipeline_state(event_id: Optional[int] = None) -> str:
        """Get the full graphics pipeline state at the current or specified event.

        Returns topology, viewports, scissors, rasterizer, blend, depth, stencil state,
        bound shaders, and output targets.

        Args:
            event_id: Optional event ID to navigate to first. Uses current event if omitted.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.ensure_event(event_id)
        if err:
            return to_json(err)

        state = session.controller.GetPipelineState()
        result = _serialize_pipeline_state(state)
        result["event_id"] = session.current_event
        return to_json(result)

    @mcp.tool()
    def get_shader_bindings(stage: str, event_id: Optional[int] = None) -> str:
        """Get resource bindings for a specific shader stage at the current event.

        Shows constant buffers, shader resource views (SRVs), UAVs, and samplers.

        Args:
            stage: Shader stage name (vertex, hull, domain, geometry, pixel, compute).
            event_id: Optional event ID to navigate to first.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.ensure_event(event_id)
        if err:
            return to_json(err)

        stage_enum = SHADER_STAGE_MAP.get(stage.lower())
        if stage_enum is None:
            return to_json(make_error(f"Unknown shader stage: {stage}. Valid: {list(SHADER_STAGE_MAP.keys())}", "API_ERROR"))

        state = session.controller.GetPipelineState()
        refl = state.GetShaderReflection(stage_enum)
        if refl is None:
            return to_json(make_error(f"No shader bound at stage '{stage}'", "API_ERROR"))

        bindings: dict = {"stage": stage, "event_id": session.current_event}

        # Constant buffers
        cbs = []
        for i, cb_refl in enumerate(refl.constantBlocks):
            try:
                cb_bind = state.GetConstantBlock(stage_enum, i, 0)
                cbs.append({
                    "index": i,
                    "name": cb_refl.name,
                    "byte_size": cb_refl.byteSize,
                    "resource_id": str(cb_bind.descriptor.resource),
                })
            except Exception:
                cbs.append({"index": i, "name": cb_refl.name, "error": "failed to read binding"})
        bindings["constant_buffers"] = cbs

        # Read-only resources (SRVs) - fetch all at once, match by access.index
        ros = []
        try:
            all_ro_binds = state.GetReadOnlyResources(stage_enum)
            # Build lookup: reflection index -> list of descriptors
            ro_by_index: dict = {}
            for b in all_ro_binds:
                ro_by_index.setdefault(b.access.index, []).append(b)
        except Exception as e:
            all_ro_binds = None
            ro_error = f"{type(e).__name__}: {e}"

        for i, ro_refl in enumerate(refl.readOnlyResources):
            if all_ro_binds is None:
                ros.append({"index": i, "name": ro_refl.name, "error": f"failed to read bindings: {ro_error}"})
                continue
            entries = []
            for b in ro_by_index.get(i, []):
                entries.append({"resource_id": str(b.descriptor.resource)})
            ros.append({
                "index": i,
                "name": ro_refl.name,
                "type": str(ro_refl.textureType),
                "bindings": entries,
            })
        bindings["read_only_resources"] = ros

        # Read-write resources (UAVs)
        rws = []
        try:
            all_rw_binds = state.GetReadWriteResources(stage_enum)
            rw_by_index: dict = {}
            for b in all_rw_binds:
                rw_by_index.setdefault(b.access.index, []).append(b)
        except Exception as e:
            all_rw_binds = None
            rw_error = f"{type(e).__name__}: {e}"

        for i, rw_refl in enumerate(refl.readWriteResources):
            if all_rw_binds is None:
                rws.append({"index": i, "name": rw_refl.name, "error": f"failed to read bindings: {rw_error}"})
                continue
            entries = []
            for b in rw_by_index.get(i, []):
                entries.append({"resource_id": str(b.descriptor.resource)})
            rws.append({
                "index": i,
                "name": rw_refl.name,
                "type": str(rw_refl.textureType),
                "bindings": entries,
            })
        bindings["read_write_resources"] = rws

        # Samplers
        samplers = []
        try:
            all_sampler_binds = state.GetSamplers(stage_enum)
            s_by_index: dict = {}
            for b in all_sampler_binds:
                s_by_index.setdefault(b.access.index, []).append(b)
        except Exception as e:
            all_sampler_binds = None
            s_error = f"{type(e).__name__}: {e}"

        for i, s_refl in enumerate(refl.samplers):
            if all_sampler_binds is None:
                samplers.append({"index": i, "name": s_refl.name, "error": f"failed to read bindings: {s_error}"})
                continue
            entries = []
            for b in s_by_index.get(i, []):
                entries.append({"resource_id": str(b.descriptor.resource)})
            samplers.append({
                "index": i,
                "name": s_refl.name,
                "bindings": entries,
            })
        bindings["samplers"] = samplers

        return to_json(bindings)

    @mcp.tool()
    def get_vertex_inputs(event_id: Optional[int] = None) -> str:
        """Get vertex input layout and buffer bindings at the current event.

        Shows vertex attributes (name, format, offset), vertex buffer bindings, and index buffer.

        Args:
            event_id: Optional event ID to navigate to first.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)
        err = session.ensure_event(event_id)
        if err:
            return to_json(err)

        state = session.controller.GetPipelineState()

        # Index buffer
        ib = state.GetIBuffer()
        ib_info = {
            "resource_id": str(ib.resourceId),
            "byte_offset": ib.byteOffset,
            "byte_stride": ib.byteStride,
        }

        # Vertex buffers
        vbs = state.GetVBuffers()
        vb_list = []
        for i, vb in enumerate(vbs):
            if int(vb.resourceId) == 0:
                continue
            vb_list.append({
                "slot": i,
                "resource_id": str(vb.resourceId),
                "byte_offset": vb.byteOffset,
                "byte_stride": vb.byteStride,
            })

        # Vertex attributes
        attrs = state.GetVertexInputs()
        attr_list = []
        for a in attrs:
            attr_list.append({
                "name": a.name,
                "vertex_buffer": a.vertexBuffer,
                "byte_offset": a.byteOffset,
                "per_instance": a.perInstance,
                "instance_rate": a.instanceRate,
                "format": str(a.format.Name()),
            })

        result = {
            "event_id": session.current_event,
            "index_buffer": ib_info,
            "vertex_buffers": vb_list,
            "vertex_attributes": attr_list,
        }
        return to_json(result)

    @mcp.tool()
    def get_draw_call_state(event_id: int) -> str:
        """Get complete draw call state in a single call.

        Returns action info, blend, depth, stencil, rasterizer, bound textures,
        render targets, and shader summaries — everything needed to understand a draw call.

        Args:
            event_id: The event ID of the draw call to inspect.
        """
        session = get_session()
        err = session.require_open()
        if err:
            return to_json(err)

        result = _get_draw_state_dict(session, event_id)
        if "error" in result:
            return to_json(result)
        return to_json(result)


def _get_draw_state_dict(session, event_id: int) -> dict:
    """Internal helper: build a complete draw call state dict.

    Shared by get_draw_call_state and diff_draw_calls.
    """
    err = session.set_event(event_id)
    if err:
        return err

    action = session.get_action(event_id)
    if action is None:
        return make_error(f"Event ID {event_id} not found", "INVALID_EVENT_ID")

    sf = session.structured_file
    state = session.controller.GetPipelineState()

    warnings: list[str] = []

    result: dict = {
        "event_id": event_id,
        "action_name": action.GetName(sf),
        "vertex_count": action.numIndices,
        "instance_count": action.numInstances,
    }

    # Topology
    try:
        topo = state.GetPrimitiveTopology()
        result["topology"] = enum_str(topo, TOPOLOGY_MAP, "Topology.")
    except Exception as e:
        result["topology"] = "Unknown"
        warnings.append(f"topology: {type(e).__name__}: {e}")

    # Blend state (first enabled blend target)
    try:
        cb = state.GetColorBlend()
        if cb.blends:
            b = cb.blends[0]
            color_eq = _serialize_blend_eq(b.colorBlend)
            alpha_eq = _serialize_blend_eq(b.alphaBlend)
            blend_info: dict = {
                "enabled": b.enabled,
                "color_src": color_eq["source"],
                "color_dst": color_eq["destination"],
                "color_op": color_eq["operation"],
                "alpha_src": alpha_eq["source"],
                "alpha_dst": alpha_eq["destination"],
                "alpha_op": alpha_eq["operation"],
            }
            if b.enabled:
                blend_info["formula"] = blend_formula(
                    color_eq["source"], color_eq["destination"], color_eq["operation"],
                    alpha_eq["source"], alpha_eq["destination"], alpha_eq["operation"],
                )
            result["blend"] = blend_info
    except Exception as e:
        warnings.append(f"blend: {type(e).__name__}: {e}")

    # Depth state
    try:
        ds = state.GetDepthState()
        result["depth"] = {
            "test": ds.depthEnable,
            "write": ds.depthWrites,
            "func": enum_str(ds.depthFunction, COMPARE_FUNC_MAP, "CompareFunc."),
        }
    except Exception as e:
        warnings.append(f"depth: {type(e).__name__}: {e}")

    # Stencil state
    try:
        ss = state.GetStencilState()
        result["stencil"] = {"enabled": ss.stencilEnable}
    except Exception as e:
        warnings.append(f"stencil: {type(e).__name__}: {e}")

    # Rasterizer
    try:
        raster = state.GetRasterizer()
        result["rasterizer"] = {
            "cull": enum_str(raster.cullMode, CULL_MODE_MAP, "CullMode."),
            "fill": enum_str(raster.fillMode, FILL_MODE_MAP, "FillMode."),
            "front_ccw": raster.frontCCW,
        }
    except Exception as e:
        warnings.append(f"rasterizer: {type(e).__name__}: {e}")

    # Textures bound to pixel shader
    textures = []
    try:
        ps_refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
        if ps_refl is not None:
            all_ro = state.GetReadOnlyResources(rd.ShaderStage.Pixel)
            ro_by_index: dict = {}
            for b in all_ro:
                ro_by_index.setdefault(b.access.index, []).append(b)
            for i, ro_refl in enumerate(ps_refl.readOnlyResources):
                for b in ro_by_index.get(i, []):
                    rid_str = str(b.descriptor.resource)
                    tex_desc = session.get_texture_desc(rid_str)
                    tex_entry: dict = {
                        "slot": i,
                        "name": ro_refl.name,
                        "resource_id": rid_str,
                    }
                    if tex_desc is not None:
                        tex_entry["size"] = f"{tex_desc.width}x{tex_desc.height}"
                        tex_entry["format"] = str(tex_desc.format.Name())
                    textures.append(tex_entry)
    except Exception as e:
        warnings.append(f"textures: {type(e).__name__}: {e}")
    result["textures"] = textures

    # Render targets
    render_targets = []
    try:
        outputs = state.GetOutputTargets()
        for o in outputs:
            if int(o.resource) == 0:
                continue
            rid_str = str(o.resource)
            rt_entry: dict = {"resource_id": rid_str}
            tex_desc = session.get_texture_desc(rid_str)
            if tex_desc is not None:
                rt_entry["size"] = f"{tex_desc.width}x{tex_desc.height}"
                rt_entry["format"] = str(tex_desc.format.Name())
            render_targets.append(rt_entry)
    except Exception as e:
        warnings.append(f"render_targets: {type(e).__name__}: {e}")
    result["render_targets"] = render_targets

    # Depth target
    try:
        dt = state.GetDepthTarget()
        if int(dt.resource) != 0:
            rid_str = str(dt.resource)
            dt_entry: dict = {"resource_id": rid_str}
            tex_desc = session.get_texture_desc(rid_str)
            if tex_desc is not None:
                dt_entry["size"] = f"{tex_desc.width}x{tex_desc.height}"
                dt_entry["format"] = str(tex_desc.format.Name())
            result["depth_target"] = dt_entry
    except Exception as e:
        warnings.append(f"depth_target: {type(e).__name__}: {e}")

    if warnings:
        result["warnings"] = warnings

    # Shader summaries
    shaders = {}
    for sname, sstage in SHADER_STAGE_MAP.items():
        if sstage == rd.ShaderStage.Compute:
            continue
        refl = state.GetShaderReflection(sstage)
        if refl is None:
            continue
        info: dict = {
            "resource_id": str(refl.resourceId),
            "entry_point": state.GetShaderEntryPoint(sstage),
        }
        if sname == "vertex":
            info["inputs"] = [s.semanticIdxName or s.varName for s in refl.inputSignature]
        if sname == "pixel":
            info["texture_count"] = len(refl.readOnlyResources)
        shaders[sname] = info
    result["shaders"] = shaders

    return result
