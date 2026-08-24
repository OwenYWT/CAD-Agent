from __future__ import annotations

import asyncio
import inspect
import logging
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.agent.failure_taxonomy import FixPath, classify
from app.agent.run_steps import ensure_timeline_fields
from app.agent.step_runner import decide_next_step
from app.agent.visual_gate import refine_visually
from app.models.schemas import BoundingBox, GenerateResponse, RepairStep, StepUpdate, ValidationResult
from app.sandbox.code_analyzer import analyze_code
from app.sandbox.code_filter import validate_code
from app.validation.inspect import build_inspect_report

logger = logging.getLogger(__name__)


class ExecutionPhase(str, Enum):
    VALIDATE_CODE = "validate_code"
    STATIC_ANALYSIS = "static_analysis"
    EXECUTE_CODE = "execute_code"
    GEOMETRY_VALIDATE = "geometry_validate"
    VISION_VALIDATE = "vision_validate"
    REPAIR_CODE = "repair_code"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class ExecutionTraceItem:
    phase: ExecutionPhase
    attempt: int
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class _RefinedBuild:
    """A candidate the visual loop proved better, re-derived for publication."""

    result: Any
    files: dict[str, str]
    params: dict[str, Any]
    validation: Any | None
    inspect_report: Any | None


async def _already_repaired(source_code: str) -> str:
    """Adapt an in-hand result to the awaitable ``_persist_repair_step`` expects.

    The refinement loop has already produced and validated the repaired source,
    so the persisted repair step records it rather than generating it again.
    """
    return source_code


class _RetryAfterVisualRepair:
    """Sentinel: the visual gate rewrote the code and wants a fresh attempt."""

    __slots__ = ()


_RETRY_AFTER_VISUAL_REPAIR = _RetryAfterVisualRepair()


async def _validate_visually(
    validator: Any,
    *,
    user_prompt: str,
    render_paths: Any,
    code: str,
    model_path: Any = None,
    plan: Any = None,
) -> Any:
    """Call a visual validator, offering the mesh only if it accepts one.

    The real validator measures the mesh when handed one, which is what lets it
    settle size and hole count instead of asking a model to read them off a
    picture. Test doubles and any third-party validator implement only the
    original three-argument call, so the extra evidence is offered by inspecting
    the signature rather than by catching TypeError -- which would also swallow a
    genuine TypeError raised inside the validator.
    """
    extra: dict[str, Any] = {}
    try:
        accepted = inspect.signature(validator.validate).parameters
        if "model_path" in accepted:
            extra["model_path"] = model_path
        if "plan" in accepted:
            extra["plan"] = plan
    except (TypeError, ValueError):  # builtins and C callables have no signature
        pass
    return await validator.validate(user_prompt, render_paths, code, **extra)


@dataclass
class ExecutionStateMachine:
    orchestrator: Any
    request_id: str
    code: str
    plan: Any | None
    output_formats: list[str]
    on_step: Any = None
    user_prompt: str = ""
    is_2d: bool = False
    max_retries: int = 5
    run_id: str | None = None
    trace: list[ExecutionTraceItem] = field(default_factory=list)

    attempt: int = 1
    vision_retry_count: int = 0
    last_error_sig: str | None = None
    repeat_error_count: int = 0
    failure_retries: dict[str, int] = field(default_factory=dict)
    repair_history: list[RepairStep] = field(default_factory=list)

    def _trace(self, phase: ExecutionPhase, message: str, *, detail: dict[str, Any] | None = None) -> None:
        self.trace.append(
            ExecutionTraceItem(
                phase=phase,
                attempt=self.attempt,
                message=message,
                detail=detail or {},
            )
        )

    async def _emit_step(self, step: str, message: str) -> None:
        if not self.on_step:
            return

        payload = ensure_timeline_fields(StepUpdate(step=step, message=message))
        result = self.on_step(payload)
        if asyncio.iscoroutine(result):
            await result

    async def _start_persisted_step(self, step_type: str, input_data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self.run_id:
            return None
        return await self.orchestrator.run_store.start_step(self.run_id, step_type, input_data or {})

    async def _complete_persisted_step(
        self,
        step: dict[str, Any] | None,
        output_data: dict[str, Any] | None = None,
        *,
        status: str = "succeeded",
    ) -> None:
        if not step:
            return
        await self.orchestrator.run_store.complete_step(step["id"], output_data or {}, status=status)
        await self._trace_next_step_decision()

    async def _fail_persisted_step(
        self,
        step: dict[str, Any] | None,
        error_data: dict[str, Any] | None = None,
        *,
        status: str = "failed",
    ) -> None:
        if not step:
            return
        await self.orchestrator.run_store.fail_step(step["id"], error_data or {"message": "\u6b65\u9aa4\u6267\u884c\u5931\u8d25"}, status=status)
        await self._trace_next_step_decision()

    async def _trace_next_step_decision(self) -> None:
        if not self.run_id:
            return
        steps = await self.orchestrator.run_store.list_steps(self.run_id)
        decision = decide_next_step(steps)
        if decision.step_type:
            self._trace(
                ExecutionPhase.COMPLETE,
                "\u5df2\u6839\u636e\u5de5\u5177\u7ed3\u679c\u51b3\u5b9a\u4e0b\u4e00\u6b65",
                detail={
                    "next_step": decision.step_type,
                    "reason": decision.reason,
                    "input": decision.input_data,
                },
            )

    async def _persist_repair_step(
        self,
        *,
        stage: str,
        error_type: str,
        message: str,
        action: str,
        repair_coro: Any,
        prev_code: str | None = None,
    ) -> Any:
        step = await self._start_persisted_step(
            "repair_code",
            {
                "attempt": self.attempt,
                "stage": stage,
                "error_type": error_type,
                "message": message,
                "action": action,
            },
        )
        try:
            repaired = await repair_coro
        except Exception as exc:
            await self._fail_persisted_step(
                step,
                {"type": exc.__class__.__name__, "message": str(exc), "stage": stage, "action": action},
            )
            raise

        code_changed = None
        if isinstance(repaired, str) and prev_code is not None:
            code_changed = repaired.strip() != prev_code.strip()
        await self._complete_persisted_step(
            step,
            {
                "attempt": self.attempt,
                "stage": stage,
                "action": action,
                "status": "repaired",
                "code_changed": code_changed,
                "repaired_code": repaired if isinstance(repaired, str) else None,
            },
        )
        return repaired

    async def _run_visual_gate(
        self,
        *,
        result: Any,
        stl_path: Any,
        inspect_report: Any,
        attempt: int,
    ) -> "_RefinedBuild | _RetryAfterVisualRepair | None":
        """Inspect the produced model and act on what the inspection found.

        Two modes share this one entry point. The primary mode is the VLM
        refinement loop, which renders, measures, critiques and repairs across
        several candidates and returns the best it can prove. When refinement is
        not configured -- no vision credentials, or it is switched off -- the
        gate degrades to a single-shot judgement over the injected renderer and
        validator instead of silently skipping inspection altogether.
        """
        await self._emit_step("executing", "正在运行视觉校验...")
        self._trace(ExecutionPhase.VISION_VALIDATE, "正在运行视觉校验")

        async def _progress(message: str) -> None:
            await self._emit_step("fixing_error", message)

        try:
            refinement = await refine_visually(
                source_code=self.code,
                objective=self.user_prompt or (self.plan.description if self.plan else ""),
                executor=self.orchestrator.executor,
                work_dir=Path(result.work_dir) / "visual-refine",
                plan=self.plan,
                model_path=Path(stl_path),
                on_progress=_progress,
            )
        except Exception as exc:  # pragma: no cover - refinement is best-effort
            logger.warning("Visual refinement failed: %s", exc)
            refinement = None

        if refinement is None:
            return await self._judge_visually_once(
                result=result,
                stl_path=stl_path,
                inspect_report=inspect_report,
                attempt=attempt,
            )

        outcome, builder = refinement
        self._trace(
            ExecutionPhase.VISION_VALIDATE,
            outcome.summary_line(),
            detail={"status": outcome.status, "iterations": outcome.iterations},
        )

        if outcome.status == "indeterminate":
            reason = (
                outcome.critique.indeterminate_reason
                if outcome.critique is not None
                else "Vision validation was indeterminate"
            )
            self.orchestrator._add_indeterminate_vision_check(inspect_report, reason)
            return None

        # A defect the loop could not clear is still a finding. Reporting it as a
        # warning keeps the result honest instead of shipping a known-wrong model
        # with a clean inspect report.
        if outcome.critique is not None and outcome.critique.is_match is False:
            self.orchestrator._add_indeterminate_vision_check(
                inspect_report,
                "视觉校验未通过: "
                + (
                    "; ".join(
                        check.observed or check.requirement
                        for check in outcome.critique.failures[:3]
                    )
                    or "模型与需求不一致"
                ),
            )

        if not outcome.changed:
            return None

        rebuilt = builder.result_for(outcome.source_code)
        if rebuilt is None or not getattr(rebuilt, "success", False):
            # The loop reported a better candidate but its build is not available
            # to publish. Keeping the current one is the honest outcome.
            return None

        issues = "; ".join(
            check.observed or check.requirement
            for check in (outcome.critique.failures if outcome.critique else ())
        )
        self.repair_history.append(
            RepairStep(
                attempt=attempt,
                stage="vision",
                error_type="VisionMismatch",
                message=issues or "visual refinement improved the model",
                action="fix_visual_issues",
                status="repaired",
            )
        )
        prev_code = self.code
        self.code = await self._persist_repair_step(
            stage="vision",
            error_type="VisionMismatch",
            message=issues or "visual refinement improved the model",
            action="fix_visual_issues",
            prev_code=prev_code,
            repair_coro=_already_repaired(outcome.source_code),
        )
        return await self._adopt_refined_build(rebuilt, inspect_report)

    async def _judge_visually_once(
        self,
        *,
        result: Any,
        stl_path: Any,
        inspect_report: Any,
        attempt: int,
    ) -> "_RetryAfterVisualRepair | None":
        """Single-shot visual gate: render, judge once, repair once if needed.

        This is the degraded mode used when the refinement loop is unavailable.
        It reaches the provider through the orchestrator's renderer and
        validator, which is also the seam the hermetic end-to-end harness
        injects, so the gate stays exercised in tests that have no provider.
        """
        renderer = getattr(self.orchestrator, "renderer", None)
        validator = getattr(self.orchestrator, "vision_validator", None)
        if renderer is None or validator is None:
            self.orchestrator._add_indeterminate_vision_check(
                inspect_report, "Visual validation is not configured"
            )
            return None
        if self.vision_retry_count >= 2:
            return None

        try:
            render_paths = renderer.render_stl(stl_path, result.work_dir / "renders")
            if not render_paths:
                self.orchestrator._add_indeterminate_vision_check(
                    inspect_report, "No render images were generated"
                )
                return None

            judgment = await _validate_visually(
                validator,
                user_prompt=self.user_prompt,
                render_paths=render_paths,
                code=self.code,
                model_path=stl_path,
                plan=self.plan,
            )
        except Exception as exc:  # pragma: no cover - best-effort validation
            logger.warning("Vision validation skipped: %s", exc)
            self.orchestrator._add_indeterminate_vision_check(
                inspect_report, "Vision validation was skipped"
            )
            return None

        if judgment.is_match is None:
            self.orchestrator._add_indeterminate_vision_check(
                inspect_report,
                "; ".join(judgment.issues) or "Vision validation was indeterminate",
            )
            return None
        if judgment.is_match or attempt >= self.max_retries:
            return None

        message = "; ".join(judgment.issues)
        self.vision_retry_count += 1
        self._trace(
            ExecutionPhase.REPAIR_CODE,
            "Vision mismatch, repairing",
            detail={"error_type": "VisionMismatch", "message": message},
        )
        await self._emit_step("fixing_error", "视觉校验失败，正在自动修复...")
        self.repair_history.append(
            RepairStep(
                attempt=attempt,
                stage="vision",
                error_type="VisionMismatch",
                message=message,
                action="fix_visual_issues",
                status="repaired",
            )
        )
        prev_code = self.code
        self.code = await self._persist_repair_step(
            stage="vision",
            error_type="VisionMismatch",
            message=message,
            action="fix_visual_issues",
            prev_code=prev_code,
            repair_coro=self.orchestrator.code_gen.fix_visual_issues(
                self.code,
                judgment.issues,
                judgment.suggestions,
                on_step=self.on_step,
            ),
        )
        return _RETRY_AFTER_VISUAL_REPAIR

    async def _adopt_refined_build(
        self, rebuilt: Any, inspect_report: Any
    ) -> "_RefinedBuild":
        """Re-derive files, params and geometry facts for an adopted candidate."""
        files = self.orchestrator._copy_output_files(
            rebuilt.work_dir, self.request_id, self.output_formats
        )
        params = self.orchestrator._extract_params(self.code)
        validation_data = None
        refreshed_report = inspect_report

        stl_path = self.orchestrator._find_stl_in_output(rebuilt.work_dir)
        if stl_path:
            expected_dims = self.plan.dimensions if self.plan else None
            geometry = await self.orchestrator.geometry_validator.validate(
                stl_path, expected_dims
            )
            validation_data = ValidationResult(
                is_watertight=geometry.is_watertight,
                bounding_box=BoundingBox(**geometry.bounding_box),
                volume=geometry.volume,
                printable=geometry.printable,
                fits_build_volume=geometry.fits_build_volume,
                min_wall_thickness=geometry.min_wall_thickness,
                print_warnings=geometry.print_warnings,
            )
            refreshed_report = build_inspect_report(
                geometry,
                available_exports=sorted(files.keys()),
                repair_attempts=len(self.repair_history),
                source="geometry_validator",
            )
        return _RefinedBuild(
            result=rebuilt,
            files=files,
            params=params,
            validation=validation_data,
            inspect_report=refreshed_report,
        )

    async def _finalize_success(self, response: GenerateResponse) -> GenerateResponse:
        step = await self._start_persisted_step(
            "finalize_result",
            {"request_id": response.request_id, "success": True},
        )
        await self._complete_persisted_step(
            step,
            {
                "request_id": response.request_id,
                "success": True,
                "attempts": response.attempts,
                "files": sorted((response.files or {}).keys()),
            },
        )
        return response

    async def _finalize_failure(self, response: GenerateResponse) -> GenerateResponse:
        step = await self._start_persisted_step(
            "finalize_result",
            {"request_id": response.request_id, "success": False},
        )
        await self._fail_persisted_step(
            step,
            response.error or {"message": "\u6267\u884c\u5931\u8d25"},
        )
        return response

    def _failure_response(self, result: Any | None) -> GenerateResponse:
        return GenerateResponse(
            request_id=self.request_id,
            success=False,
            code=self.code,
            error={
                "type": getattr(result, "error_type", None) or "ExecutionError",
                "message": getattr(result, "error_message", None) or "\u5df2\u8fbe\u5230\u6700\u5927\u91cd\u8bd5\u6b21\u6570",
            },
            execution_time_ms=getattr(result, "execution_time_ms", 0) if result else 0,
            attempts=self.attempt,
            repair_history=self.repair_history,
            design_brief=self.plan.design_brief if self.plan else None,
        )

    async def run(self) -> GenerateResponse:
        result = None

        for attempt in range(1, self.max_retries + 1):
            self.attempt = attempt

            try:
                self._trace(ExecutionPhase.VALIDATE_CODE, "Validating code")
                is_valid, error_msg = validate_code(self.code)
                if not is_valid:
                    if attempt < self.max_retries:
                        self._trace(
                            ExecutionPhase.REPAIR_CODE,
                            "\u4ee3\u7801\u6821\u9a8c\u5931\u8d25\uff0c\u6b63\u5728\u4fee\u590d",
                            detail={"error_type": "ValidationError", "message": error_msg},
                        )
                        await self._emit_step(
                            "fixing_error",
                            f"\u4ee3\u7801\u6821\u9a8c\u5931\u8d25\uff0c\u6b63\u5728\u81ea\u52a8\u4fee\u590d...\uff08\u7b2c {attempt}/{self.max_retries} \u6b21\uff09",
                        )
                        self.repair_history.append(
                            RepairStep(
                                attempt=attempt,
                                stage="validation",
                                error_type="ValidationError",
                                message=error_msg or "\u4ee3\u7801\u6821\u9a8c\u5931\u8d25",
                                action="fix_error",
                                status="repaired",
                            )
                        )
                        prev_code = self.code
                        self.code = await self._persist_repair_step(
                            stage="validation",
                            error_type="ValidationError",
                            message=error_msg or "\u4ee3\u7801\u6821\u9a8c\u5931\u8d25",
                            action="fix_error",
                            prev_code=prev_code,
                            repair_coro=self.orchestrator.code_gen.fix_error(
                                self.code,
                                {"type": "ValidationError", "message": error_msg, "gate": "ValidationError"},
                                self.plan,
                            ),
                        )
                        continue

                    self._trace(
                        ExecutionPhase.FAILED,
                        "\u4ee3\u7801\u6821\u9a8c\u5931\u8d25\uff0c\u4e14\u91cd\u8bd5\u6b21\u6570\u5df2\u7528\u5c3d",
                        detail={"error_type": "ValidationError", "message": error_msg},
                    )
                    return await self._finalize_failure(GenerateResponse(
                        request_id=self.request_id,
                        success=False,
                        code=self.code,
                        error={"type": "ValidationError", "message": error_msg},
                        attempts=attempt,
                        repair_history=self.repair_history,
                        design_brief=self.plan.design_brief if self.plan else None,
                    ))

                self._trace(ExecutionPhase.STATIC_ANALYSIS, "Running static analysis")
                analysis_warnings = analyze_code(self.code)
                if analysis_warnings and attempt < self.max_retries:
                    logger.info("Static analysis warnings: %s", analysis_warnings)
                    self._trace(
                        ExecutionPhase.REPAIR_CODE,
                        "\u9759\u6001\u5206\u6790\u53d1\u73b0\u95ee\u9898\uff0c\u6b63\u5728\u4fee\u590d",
                        detail={"warnings": analysis_warnings},
                    )
                    await self._emit_step(
                        "fixing_error",
                        "\u9759\u6001\u5206\u6790\u53d1\u73b0\u95ee\u9898\uff0c\u6b63\u5728\u81ea\u52a8\u4fee\u590d...",
                    )
                    self.repair_history.append(
                        RepairStep(
                            attempt=attempt,
                            stage="static_analysis",
                            error_type="StaticAnalysis",
                            message="; ".join(analysis_warnings),
                            action="fix_error",
                            status="repaired",
                        )
                    )
                    prev_code = self.code
                    self.code = await self._persist_repair_step(
                        stage="static_analysis",
                        error_type="StaticAnalysis",
                        message="; ".join(analysis_warnings),
                        action="fix_error",
                        prev_code=prev_code,
                        repair_coro=self.orchestrator.code_gen.fix_error(
                            self.code,
                            {
                                "type": "StaticAnalysis",
                                "message": "; ".join(analysis_warnings),
                                "gate": "StaticAnalysis",
                            },
                            self.plan,
                        ),
                    )
                    continue

                self._trace(
                    ExecutionPhase.EXECUTE_CODE,
                    "Executing code",
                    detail={"mode": "2d" if self.is_2d else "3d"},
                )
                await self._emit_step(
                    "executing",
                    f"Executing code... (attempt {attempt}/{self.max_retries})",
                )

                exec_mode = "2d" if self.is_2d else "3d"
                execute_step = await self._start_persisted_step(
                    "execute_cad_code",
                    {
                        "attempt": attempt,
                        "mode": exec_mode,
                        "code": self.code,
                        "code_preview": self.code[:500],
                        "output_formats": self.output_formats,
                        "request_id": self.request_id,
                        "user_prompt": self.user_prompt,
                        "is_2d": self.is_2d,
                    },
                )
                result = await self.orchestrator.executor.execute(self.code, mode=exec_mode)
                if result.success:
                    await self._complete_persisted_step(
                        execute_step,
                        {"attempt": attempt, "mode": exec_mode, "execution_time_ms": result.execution_time_ms},
                    )
                else:
                    await self._fail_persisted_step(
                        execute_step,
                        {
                            "type": result.error_type or "ExecutionError",
                            "message": result.error_message or "\u6267\u884c\u5931\u8d25",
                            "traceback": result.traceback,
                        },
                    )

                if result.success:
                    files = self.orchestrator._copy_output_files(result.work_dir, self.request_id, self.output_formats)
                    params = self.orchestrator._extract_params(self.code)

                    if self.is_2d:
                        dxf_path = self.orchestrator._find_file_in_output(result.work_dir, ".dxf")
                        if dxf_path and "dxf" not in files:
                            from pathlib import Path

                            from app.config import settings

                            dest_dir = Path(settings.file_storage_dir) / self.request_id
                            dest_dir.mkdir(parents=True, exist_ok=True)
                            dest_dxf = dest_dir / dxf_path.name
                            shutil.copy2(dxf_path, dest_dxf)
                            files["dxf"] = f"/api/files/{self.request_id}/{dxf_path.name}"

                        if dxf_path:
                            try:
                                from pathlib import Path

                                from app.config import settings
                                from app.rendering.dxf_renderer import dxf_to_svg

                                svg_dir = Path(settings.file_storage_dir) / self.request_id
                                svg_dir.mkdir(parents=True, exist_ok=True)
                                svg_path = svg_dir / "result.svg"
                                dxf_to_svg(dxf_path, svg_path)
                                files["svg"] = f"/api/files/{self.request_id}/result.svg"
                            except Exception as exc:  # pragma: no cover - rendering failures are non-fatal
                                logger.warning("SVG generation failed: %s", exc)

                        self._trace(
                            ExecutionPhase.COMPLETE,
                            "2D \u6267\u884c\u6210\u529f",
                            detail={"files": sorted(files.keys())},
                        )
                        return await self._finalize_success(GenerateResponse(
                            request_id=self.request_id,
                            success=True,
                            files=files,
                            code=self.code,
                            params=params if params else None,
                            execution_time_ms=result.execution_time_ms,
                            attempts=self.attempt,
                            repair_history=self.repair_history,
                        ))

                    validation_data = None
                    inspect_report = None
                    stl_path = self.orchestrator._find_stl_in_output(result.work_dir)
                    if stl_path:
                        expected_dims = self.plan.dimensions if self.plan else None
                        geo_validation = await self.orchestrator.geometry_validator.validate(stl_path, expected_dims)
                        self._trace(
                            ExecutionPhase.GEOMETRY_VALIDATE,
                            "Geometry validation completed",
                            detail={"passed": geo_validation.passed, "watertight": geo_validation.is_watertight},
                        )
                        validation_data = ValidationResult(
                            is_watertight=geo_validation.is_watertight,
                            bounding_box=BoundingBox(**geo_validation.bounding_box),
                            volume=geo_validation.volume,
                            printable=geo_validation.printable,
                            fits_build_volume=geo_validation.fits_build_volume,
                            min_wall_thickness=geo_validation.min_wall_thickness,
                            print_warnings=geo_validation.print_warnings,
                        )
                        inspect_report = build_inspect_report(
                            geo_validation,
                            available_exports=sorted(files.keys()),
                            repair_attempts=len(self.repair_history),
                            source="geometry_validator",
                        )

                        if not geo_validation.passed and attempt < self.max_retries:
                            error_messages = "; ".join(rule.message for rule in geo_validation.rules if not rule.passed)
                            self._trace(
                                ExecutionPhase.REPAIR_CODE,
                                "\u51e0\u4f55\u6821\u9a8c\u5931\u8d25\uff0c\u6b63\u5728\u4fee\u590d",
                                detail={"error_type": "GeometryError", "message": error_messages},
                            )
                            await self._emit_step(
                                "fixing_error",
                                f"\u51e0\u4f55\u6821\u9a8c\u5931\u8d25\uff1a{error_messages}",
                            )
                            self.repair_history.append(
                                RepairStep(
                                    attempt=attempt,
                                    stage="geometry",
                                    error_type="GeometryError",
                                    message=error_messages,
                                    action="fix_error",
                                    status="repaired",
                                )
                            )
                            prev_code = self.code
                            self.code = await self._persist_repair_step(
                                stage="geometry",
                                error_type="GeometryError",
                                message=error_messages,
                                action="fix_error",
                                prev_code=prev_code,
                                repair_coro=self.orchestrator.code_gen.fix_error(
                                    self.code,
                                    {"type": "GeometryError", "message": error_messages, "gate": "GeometryError"},
                                    self.plan,
                                ),
                            )
                            continue

                        if stl_path:
                            gate = await self._run_visual_gate(
                                result=result,
                                stl_path=stl_path,
                                inspect_report=inspect_report,
                                attempt=attempt,
                            )
                            if gate is _RETRY_AFTER_VISUAL_REPAIR:
                                continue
                            if gate is not None:
                                # The refinement loop already re-executed and
                                # re-judged its own candidates, so there is
                                # nothing to retry here: adopt the build it
                                # proved best.
                                result = gate.result
                                files = gate.files
                                params = gate.params
                                validation_data = gate.validation or validation_data
                                inspect_report = gate.inspect_report or inspect_report

                    self._trace(
                        ExecutionPhase.COMPLETE,
                        "3D \u6267\u884c\u6210\u529f",
                        detail={"files": sorted(files.keys())},
                    )
                    return await self._finalize_success(GenerateResponse(
                        request_id=self.request_id,
                        success=True,
                        files=files,
                        code=self.code,
                        params=params if params else None,
                        execution_time_ms=result.execution_time_ms,
                        attempts=self.attempt,
                        validation=validation_data,
                        inspect_report=inspect_report,
                        repair_history=self.repair_history,
                    ))

                self._trace(
                    ExecutionPhase.REPAIR_CODE,
                    "\u6267\u884c\u5931\u8d25\uff0c\u6b63\u5728\u5206\u7c7b\u9519\u8bef",
                    detail={"error_type": result.error_type, "message": result.error_message},
                )
                fc = classify(result.error_type, result.error_message, result.traceback, gate="exec")
                if fc.fix_path is FixPath.HARD_STOP:
                    logger.info("Non-recoverable failure (%s), stopping retries", fc.key)
                    break

                retries_used = self.failure_retries.get(fc.key, 0)
                retry_budget = getattr(fc, "retry_budget", None)
                if (
                    retry_budget is not None
                    and retries_used >= retry_budget
                ):
                    logger.info(
                        "Retry budget exhausted for %s (%d)",
                        fc.key,
                        retry_budget,
                    )
                    break

                error_sig = fc.key
                if error_sig == self.last_error_sig:
                    self.repeat_error_count += 1
                else:
                    self.repeat_error_count = 0
                    self.last_error_sig = error_sig
                if self.repeat_error_count >= 2:
                    logger.info("Retry oscillation detected (%s x %s), stopping early", error_sig, self.repeat_error_count + 1)
                    break

                if attempt < self.max_retries:
                    await self._emit_step(
                        "fixing_error",
                        f"\u6267\u884c\u5931\u8d25\uff0c\u6b63\u5728\u81ea\u52a8\u4fee\u590d...\uff08\u7b2c {attempt}/{self.max_retries} \u6b21\uff09",
                    )
                    self.repair_history.append(
                        RepairStep(
                            attempt=attempt,
                            stage="execution",
                            error_type=result.error_type or "ExecutionError",
                            message=result.error_message or "\u6267\u884c\u5931\u8d25",
                            action="fix_error",
                            status="repaired",
                        )
                    )
                    prev_code = self.code
                    self.code = await self._persist_repair_step(
                        stage="execution",
                        error_type=result.error_type or "ExecutionError",
                        message=result.error_message or "\u6267\u884c\u5931\u8d25",
                        action="fix_error",
                        prev_code=prev_code,
                        repair_coro=self.orchestrator.code_gen.fix_error(
                            self.code,
                            {
                                "type": result.error_type,
                                "message": result.error_message,
                                "traceback": result.traceback,
                                "gate": "exec",
                            },
                            self.plan,
                        ),
                    )
                    self.failure_retries[fc.key] = retries_used + 1
                    self._trace(
                        ExecutionPhase.REPAIR_CODE,
                        "\u6267\u884c\u5931\u8d25\uff0c\u6b63\u5728\u4fee\u590d",
                        detail={"error_type": result.error_type, "message": result.error_message},
                    )
                    if self.code.strip() == prev_code.strip():
                        logger.info("fix_error returned identical code, stopping early")
                        break
                    continue

                break
            finally:
                if result is not None and getattr(result, "work_dir", None) is not None:
                    shutil.rmtree(result.work_dir, ignore_errors=True)

        self._trace(ExecutionPhase.FAILED, "Retries exhausted", detail={"attempts": self.max_retries})
        return await self._finalize_failure(self._failure_response(result))
