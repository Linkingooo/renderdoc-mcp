"""Shader analysis tools: disassemble_shader, get_shader_reflection, get_cbuffer_contents."""

from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import MCPServer

from renderdoc_mcp.session import get_session
from renderdoc_mcp.util import (
    rd,
    to_json,
    make_error,
    SHADER_STAGE_MAP,
    serialize_shader_variable,
    serialize_sig_element,
)


def _reflection_fallback(stage: str, refl) -> dict:
    """Build a fallback result from shader reflection when disassembly is unavailable."""
    result: dict = {
        "stage": stage,
        "source_type": "reflection_only",
        "resource_id": str(refl.resourceId),
        "entry_point": refl.entryPoint,
        "input_signature": [serialize_sig_element(s) for s in refl.inputSignature],
        "output_signature": [serialize_sig_element(s) for s in refl.outputSignature],
        "constant_blocks": [{"name": cb.name, "byte_size": cb.byteSize} for cb in refl.constantBlocks],
        "read_only_resources": [{"name": ro.name, "type": str(ro.textureType)} for ro in refl.readOnlyResources],
        "read_write_resources": [{"name": rw.name, "type": str(rw.textureType)} for rw in refl.readWriteResources],
        "note": "Disassembly unavailable — showing reflection data only",
    }
    return result


def register(mcp: MCPServer):
    @mcp.tool()
    def disassemble_shader(
        stage: str,
        target: Optional[str] = None,
        event_id: Optional[int] = None,
        line_range: Optional[list[int]] = None,
        search: Optional[str] = None,
    ) -> str:
        """Disassemble the shader bound at the specified stage.

        If target is omitted, tries all available targets in order and returns the
        first successful result. Falls back to reflection info if all disassembly fails.

        Args:
            stage: Shader stage (vertex, hull, domain, geometry, pixel, compute).
            target: Disassembly target/format. If omitted, tries all available targets.
            event_id: Optional event ID to navigate to first.
            line_range: [start_line, end_line] (1-based). Only return lines in this range.
            search: Keyword to search in the disassembly. Returns matching lines with
                    5 lines of context before and after each match.
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
            return to_json(make_error(f"Unknown shader stage: {stage}", "API_ERROR"))

        state = session.controller.GetPipelineState()
        refl = state.GetShaderReflection(stage_enum)
        if refl is None:
            return to_json(make_error(f"No shader bound at stage '{stage}'", "API_ERROR"))

        if stage_enum == rd.ShaderStage.Compute:
            pipe = state.GetComputePipelineObject()
        else:
            pipe = state.GetGraphicsPipelineObject()
        targets = session.controller.GetDisassemblyTargets(True)

        if not targets:
            return to_json(_reflection_fallback(stage, refl))

        disasm: str | None = None
        used_target: str | None = None

        if target is not None:
            if target not in targets:
                return to_json(make_error(
                    f"Unknown disassembly target: {target}. Available: {targets}", "API_ERROR"
                ))
            disasm = session.controller.DisassembleShader(pipe, refl, target)
            used_target = target
        else:
            for t in targets:
                try:
                    d = session.controller.DisassembleShader(pipe, refl, t)
                    if d and d.strip():
                        disasm = d
                        used_target = t
                        break
                except Exception:
                    continue

        if not disasm:
            result = _reflection_fallback(stage, refl)
            result["available_targets"] = list(targets)
            return to_json(result)

        # Apply line_range filter
        all_lines = disasm.splitlines()
        total_lines = len(all_lines)

        if line_range is not None and len(line_range) == 2:
            start = max(1, line_range[0]) - 1  # convert to 0-based
            end = min(total_lines, line_range[1])
            filtered_lines = all_lines[start:end]
            disasm_out = "\n".join(filtered_lines)
            note = f"Showing lines {start+1}–{end} of {total_lines}"
        elif search is not None:
            kw = search.lower()
            context = 5
            include: set[int] = set()
            for i, line in enumerate(all_lines):
                if kw in line.lower():
                    for j in range(max(0, i - context), min(total_lines, i + context + 1)):
                        include.add(j)
            if not include:
                note = f"No matches for '{search}' in {total_lines} lines"
                disasm_out = ""
            else:
                result_lines = []
                prev = -2
                for i in sorted(include):
                    if i > prev + 1:
                        result_lines.append("...")
                    result_lines.append(f"{i+1:4d}: {all_lines[i]}")
                    prev = i
                disasm_out = "\n".join(result_lines)
                note = f"Found '{search}' in {total_lines} total lines"
        else:
            disasm_out = disasm
            note = None

        result: dict = {
            "stage": stage,
            "target": used_target,
            "available_targets": list(targets),
            "source_type": "disasm",
            "total_lines": total_lines,
            "disassembly": disasm_out,
        }
        if note:
            result["note"] = note
        return to_json(result)

    @mcp.tool()
    def get_shader_reflection(
        stage: str,
        event_id: Optional[int] = None,
    ) -> str:
        """Get reflection information for the shader at the specified stage.

        Returns input/output signatures, constant buffer layouts, and resource bindings.

        Args:
            stage: Shader stage (vertex, hull, domain, geometry, pixel, compute).
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
            return to_json(make_error(f"Unknown shader stage: {stage}", "API_ERROR"))

        state = session.controller.GetPipelineState()
        refl = state.GetShaderReflection(stage_enum)
        if refl is None:
            return to_json(make_error(f"No shader bound at stage '{stage}'", "API_ERROR"))

        result: dict = {
            "stage": stage,
            "resource_id": str(refl.resourceId),
            "entry_point": refl.entryPoint,
        }

        # Input signature
        result["input_signature"] = [serialize_sig_element(s) for s in refl.inputSignature]
        # Output signature
        result["output_signature"] = [serialize_sig_element(s) for s in refl.outputSignature]

        # Constant blocks
        cbs = []
        for cb in refl.constantBlocks:
            cbs.append({
                "name": cb.name,
                "byte_size": cb.byteSize,
                "bind_point": cb.fixedBindNumber,
                "variables_count": len(cb.variables),
            })
        result["constant_blocks"] = cbs

        # Read-only resources
        ros = []
        for ro in refl.readOnlyResources:
            ros.append({
                "name": ro.name,
                "type": str(ro.textureType),
                "bind_point": ro.fixedBindNumber,
            })
        result["read_only_resources"] = ros

        # Read-write resources
        rws = []
        for rw in refl.readWriteResources:
            rws.append({
                "name": rw.name,
                "type": str(rw.textureType),
                "bind_point": rw.fixedBindNumber,
            })
        result["read_write_resources"] = rws

        # Samplers
        samplers = []
        for s in refl.samplers:
            samplers.append({
                "name": s.name,
                "bind_point": s.fixedBindNumber,
            })
        result["samplers"] = samplers

        return to_json(result)

    @mcp.tool()
    def get_cbuffer_contents(
        stage: str,
        cbuffer_index: int,
        event_id: Optional[int] = None,
        filter: Optional[str] = None,
    ) -> str:
        """Get the actual values of a constant buffer at the specified shader stage.

        Returns a tree of variable names and their current values.

        Args:
            stage: Shader stage (vertex, hull, domain, geometry, pixel, compute).
            cbuffer_index: Index of the constant buffer (from get_shader_reflection).
            event_id: Optional event ID to navigate to first.
            filter: Case-insensitive substring to match variable names
                    (e.g. "ibl", "exposure", "taa", "reflection"). Only matching
                    variables are returned.
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
            return to_json(make_error(f"Unknown shader stage: {stage}", "API_ERROR"))

        state = session.controller.GetPipelineState()
        refl = state.GetShaderReflection(stage_enum)
        if refl is None:
            return to_json(make_error(f"No shader bound at stage '{stage}'", "API_ERROR"))

        num_cbs = len(refl.constantBlocks)
        if num_cbs == 0:
            return to_json(make_error(
                f"Shader at stage '{stage}' has no constant buffers",
                "API_ERROR",
            ))
        if cbuffer_index < 0 or cbuffer_index >= num_cbs:
            return to_json(make_error(
                f"cbuffer_index {cbuffer_index} out of range (0-{num_cbs - 1})",
                "API_ERROR",
            ))

        if stage_enum == rd.ShaderStage.Compute:
            pipe = state.GetComputePipelineObject()
        else:
            pipe = state.GetGraphicsPipelineObject()
        entry = state.GetShaderEntryPoint(stage_enum)
        cb_bind = state.GetConstantBlock(stage_enum, cbuffer_index, 0)

        cbuffer_vars = session.controller.GetCBufferVariableContents(
            pipe, refl.resourceId, stage_enum, entry,
            cbuffer_index, cb_bind.descriptor.resource, 0, 0,
        )

        variables = [serialize_shader_variable(v) for v in cbuffer_vars]

        # Apply variable name filter
        if filter:
            kw = filter.lower()

            def _var_matches(var: dict) -> bool:
                if kw in var.get("name", "").lower():
                    return True
                for m in var.get("members", []):
                    if _var_matches(m):
                        return True
                return False

            variables = [v for v in variables if _var_matches(v)]

        return to_json({
            "stage": stage,
            "cbuffer_index": cbuffer_index,
            "cbuffer_name": refl.constantBlocks[cbuffer_index].name,
            "filter": filter,
            "variables": variables,
            "variable_count": len(variables),
        })
