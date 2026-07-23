import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.code_gen import CodeGenerator
from app.agent.design_brief import ensure_design_brief
from app.agent.failure_taxonomy import FixPath, classify
from app.agent.planner import Planner
from app.agent.run_steps import ensure_timeline_fields, make_step
from app.agent.recovery_actions import build_recovery_actions
from app.config import settings
from app.logging_context import set_request_id
from app.models.schemas import (
    AssemblyPartInfo,
    BoundingBox,
    DesignBrief,
    GenerateResponse,
    GenerationResult,
    ManufacturingProfile,
    ParamConfig,
    RepairStep,
    StepUpdate,
    ValidationResult,
)
from app.rendering.renderer import CADRenderer
from app.sandbox.code_analyzer import analyze_code
from app.sandbox.code_filter import validate_code
from app.sandbox.executor import CadQueryExecutor
from app.validation.geometry_validator import GeometryValidator
from app.validation.inspect import build_inspect_report
from app.validation.vision_validator import VisionValidator

logger = logging.getLogger(__name__)

_BLOCKING_CONFIRMATION_MARKERS = (
    "必须确认",
    "无法安全默认",
    "无法继续",
    "无法生成",
    "必须先确认",
)


def requires_design_confirmation(brief: DesignBrief) -> bool:
    """Only block generation for explicitly blocking questions.

    Normal open_questions are design brief reminders and should not interrupt
    first-pass CAD generation. This keeps the agent usable for simple prompts.
    """
    return any(
        any(marker in question for marker in _BLOCKING_CONFIRMATION_MARKERS)
        for question in brief.open_questions
    )


@dataclass
class ConversationContext:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    current_code: str | None = None
    current_params: dict | None = None
    generation_count: int = 0
    assembly_parts: list[dict] | None = None  # per-part code/metadata for assemblies
    current_design_brief: DesignBrief | None = None
    current_manufacturing_profile: ManufacturingProfile | None = None


class Orchestrator:
    MAX_RETRIES = 5

    def __init__(self):
        self.planner = Planner()
        self.code_gen = CodeGenerator()
        self.executor = CadQueryExecutor()
        self._retriever = None
        self.renderer = CADRenderer()
        self.geometry_validator = GeometryValidator()
        self.vision_validator = VisionValidator()
        from app.agent.code_cache import CodeCache
        self.code_cache = CodeCache()

    @property
    def retriever(self):
        if self._retriever is None:
            self._retriever = self._create_retriever()
        return self._retriever

    @retriever.setter
    def retriever(self, value):
        self._retriever = value

    def _create_retriever(self):
        if settings.example_retriever.strip().lower() == "vector":
            try:
                from app.examples.vector_retriever import VectorExampleRetriever

                retriever = VectorExampleRetriever()
                logger.info("Using VectorExampleRetriever (ChromaDB)")
                return retriever
            except Exception:
                logger.warning("VectorExampleRetriever unavailable, falling back to TF-IDF ExampleRetriever")

        from app.examples.retriever import ExampleRetriever

        retriever = ExampleRetriever()
        logger.info("Using TF-IDF ExampleRetriever")
        return retriever

    def _normalize_manufacturing_profile(
        self, manufacturing_profile: ManufacturingProfile | dict | None
    ) -> ManufacturingProfile | None:
        if manufacturing_profile is None:
            return None
        if isinstance(manufacturing_profile, ManufacturingProfile):
            return manufacturing_profile
        return ManufacturingProfile.model_validate(manufacturing_profile)

    def _prompt_with_manufacturing_profile(
        self, prompt: str, profile: ManufacturingProfile | None
    ) -> str:
        if not profile:
            return prompt
        return f"{profile.prompt_context()}\nUser request: {prompt}"

    def _apply_manufacturing_profile_to_brief(
        self, brief: DesignBrief, profile: ManufacturingProfile | None
    ) -> None:
        if not profile:
            return
        process_labels = {
            "fdm": "FDM",
            "sla": "SLA",
            "cnc": "CNC",
            "laser_cut": "激光切割",
            "generic": "通用工艺",
        }
        process_label = process_labels.get(profile.process, profile.process)
        brief.manufacturing_posture = f"{process_label} {profile.material}".strip()
        profile_targets = [
            f"制造工艺：{process_label}",
            f"材料：{profile.material}",
        ]
        if profile.nozzle_diameter_mm is not None:
            profile_targets.append(f"喷嘴直径：{profile.nozzle_diameter_mm:g} mm")
        if profile.layer_height_mm is not None:
            profile_targets.append(f"层高：{profile.layer_height_mm:g} mm")
        if profile.build_volume_mm:
            volume = " x ".join(f"{value:g}" for value in profile.build_volume_mm)
            profile_targets.append(f"成型空间：{volume} mm")
        for target in profile_targets:
            if target not in brief.printability_targets:
                brief.printability_targets.append(target)

    # === Stateless REST methods (Scheme C core) ===

    async def generate(
        self,
        prompt: str,
        output_formats: list[str] = None,
        on_step=None,
        manufacturing_profile: ManufacturingProfile | dict | None = None,
    ) -> GenerateResponse:
        if output_formats is None:
            output_formats = ["step", "stl"]

        request_id = str(uuid.uuid4())
        set_request_id(request_id)

        profile = self._normalize_manufacturing_profile(manufacturing_profile)
        planner_prompt = self._prompt_with_manufacturing_profile(prompt, profile)
        cache_key = planner_prompt

        # Cache hit: identical prompt already produced working code — skip the 2 LLM
        # calls (plan + codegen) and re-execute the cached code (fresh files/validation).
        cache = getattr(self, "code_cache", None)
        cached_code = cache.get(cache_key) if cache else None
        if cached_code:
            logger.info("Code cache hit — skipping LLM plan+codegen")
            if on_step:
                await _call_step(on_step, StepUpdate(step="executing", message="\u547d\u4e2d\u7f13\u5b58\uff0c\u6b63\u5728\u6267\u884c\u5df2\u6709\u6a21\u578b\u4ee3\u7801..."))
            is_2d_cached = "ezdxf" in cached_code or "result.dxf" in cached_code
            cached_result = await self._execute_with_retry(
                request_id, cached_code, None, output_formats, on_step, prompt, is_2d=is_2d_cached
            )
            if cached_result.success:
                cached_result.manufacturing_profile = profile
                return cached_result
            # cache produced stale/broken code — fall through to a fresh generation
            logger.info("Cached code failed on re-execution, regenerating")

        # Step 1: Planning
        if on_step:
            await _call_step(on_step, StepUpdate(step="planning", message="\u6b63\u5728\u7406\u89e3\u4f60\u7684\u9700\u6c42... (LLM \u89c4\u5212\u4e2d)"))

        plan = await self.planner.plan_new([{"role": "user", "content": planner_prompt}])
        plan.manufacturing_profile = profile
        design_brief = ensure_design_brief(plan)
        self._apply_manufacturing_profile_to_brief(design_brief, profile)
        logger.info(f"Plan: type={plan.part_type}, hint={plan.modeling_hint}, dims={plan.dimensions}")

        if requires_design_confirmation(design_brief):
            if on_step:
                await _call_step(on_step, StepUpdate(
                    step="planning",
                    message="检测到必须确认的关键需求，先暂停生成 CAD 模型。",
                    status="warn",
                    stage_id="design_confirmation",
                    detail={"open_questions": design_brief.open_questions},
                ))
            response = GenerateResponse(
                request_id=request_id,
                success=False,
                needs_confirmation=True,
                manufacturing_profile=profile,
                error={
                    "type": "NeedsConfirmation",
                    "message": "\u8bf7\u5148\u56de\u7b54\u8bbe\u8ba1\u7b80\u62a5\u4e2d\u7684\u5f85\u786e\u8ba4\u95ee\u9898\uff0c\u518d\u7ee7\u7eed\u751f\u6210 CAD \u6a21\u578b\u3002",
                },
                plan=plan,
                design_brief=design_brief,
            )
            response.recovery_actions = build_recovery_actions(response)
            return response

        # Step 2: Retrieve examples
        if on_step:
            await _call_step(on_step, StepUpdate(step="retrieving_examples", message="\u6b63\u5728\u67e5\u627e\u76f8\u4f3c\u53ef\u6253\u5370\u6848\u4f8b..."))

        examples = await self.retriever.find_similar(
            plan.description,
            top_k=3,
            part_type=plan.part_type,
            features=plan.features,
            modeling_hint=plan.modeling_hint or None,
        )

        # Step 3: Generate code
        is_2d = plan.part_type == "profile_2d"
        is_assembly = plan.part_type == "assembly"

        if is_2d:
            if on_step:
                await _call_step(on_step, StepUpdate(step="generating_code", message="\u6b63\u5728\u751f\u6210 ezdxf 2D \u4ee3\u7801..."))
            code = await self.code_gen.generate_2d(
                plan, examples, [{"role": "user", "content": prompt}]
            )
            output_formats = ["dxf"]
        elif is_assembly:
            if on_step:
                await _call_step(on_step, StepUpdate(step="planning", message="\u6b63\u5728\u62c6\u89e3\u88c5\u914d\u4f53\u96f6\u4ef6\u6e05\u5355..."))

            from app.agent.assembly_planner import AssemblyPlanner
            assy_planner = AssemblyPlanner()
            assy_plan = await assy_planner.plan_assembly(plan)
            total_parts = len(assy_plan.parts)
            logger.info(f"Assembly plan: {total_parts} parts — {[p.name for p in assy_plan.parts]}")

            if on_step:
                await _call_step(on_step, StepUpdate(
                    step="generating_code",
                    message=f"\u88c5\u914d\u4f53\u5171 {total_parts} \u4e2a\u96f6\u4ef6\uff0c\u6b63\u5728\u751f\u6210...",
                ))

            # Generate all parts in parallel (LLM calls), then execute sequentially
            import asyncio as _asyncio

            async def _generate_part(idx, apart):
                if on_step:
                    await _call_step(on_step, StepUpdate(
                        step="assembly_part",
                        message=f"\u6b63\u5728\u751f\u6210\u96f6\u4ef6 {idx+1}/{total_parts}: {apart.name}",
                        part_name=apart.name,
                        part_index=idx,
                        total_parts=total_parts,
                    ))
                return await self.code_gen.generate_single_part(
                    apart.name, apart.description, apart.dimensions, examples,
                )

            raw_codes = await _asyncio.gather(
                *[_generate_part(i, p) for i, p in enumerate(assy_plan.parts)]
            )

            # Validate + retry each part
            part_codes: list[dict] = []
            failed_parts: list[str] = []
            for idx, (apart, part_code) in enumerate(zip(assy_plan.parts, raw_codes)):
                part_result = await self.executor.execute(part_code)
                if part_result.success:
                    if on_step:
                        await _call_step(on_step, StepUpdate(
                            step="assembly_part",
                            message=f"\u96f6\u4ef6 {apart.name} \u9a8c\u8bc1\u901a\u8fc7",
                            part_name=apart.name,
                            part_index=idx,
                            total_parts=total_parts,
                        ))
                    shutil.rmtree(part_result.work_dir, ignore_errors=True)
                else:
                    if on_step:
                        await _call_step(on_step, StepUpdate(
                            step="fixing_error",
                            message=f"\u96f6\u4ef6 {apart.name} \u6267\u884c\u5931\u8d25\uff0c\u6b63\u5728\u4fee\u590d...",
                            part_name=apart.name,
                            part_index=idx,
                            total_parts=total_parts,
                        ))
                    shutil.rmtree(part_result.work_dir, ignore_errors=True)
                    part_code = await self.code_gen.fix_error(
                        part_code,
                        {
                            "type": part_result.error_type or "ExecutionError",
                            "message": part_result.error_message or "",
                            "traceback": part_result.traceback or "",
                        },
                        plan,
                    )
                    retry_result = await self.executor.execute(part_code)
                    shutil.rmtree(retry_result.work_dir, ignore_errors=True)
                    if not retry_result.success:
                        failed_parts.append(apart.name)
                        logger.warning(f"Part {apart.name} failed after retry: {retry_result.error_message}")

                clean_code = "\n".join(
                    line for line in part_code.splitlines()
                    if not line.strip().startswith("show_object")
                )
                part_status = "failed" if apart.name in failed_parts else "success"
                part_codes.append({
                    "name": apart.name,
                    "description": apart.description,
                    "code": clean_code,
                    "position": apart.position,
                    "color": apart.color,
                    "status": part_status,
                })

            if failed_parts and on_step:
                await _call_step(on_step, StepUpdate(
                    step="fixing_error",
                    message=f"\u8b66\u544a: \u4ee5\u4e0b\u96f6\u4ef6\u751f\u6210\u5931\u8d25: {', '.join(failed_parts)}",
                ))

            # Combine all parts into assembly
            if on_step:
                await _call_step(on_step, StepUpdate(
                    step="generating_code",
                    message="\u6b63\u5728\u7ec4\u5408\u88c5\u914d\u4f53...",
                ))
            code = await self.code_gen.generate_assembly_combiner(part_codes)
        else:
            # Multi-step decomposition for complex parts
            from app.agent.multi_step import PlanDecomposer, MultiStepExecutor
            if on_step:
                await _call_step(on_step, StepUpdate(step="generating_code", message="\u6b63\u5728\u5206\u6790\u96f6\u4ef6\u590d\u6742\u5ea6..."))
            decomposer = PlanDecomposer()
            build_plan = await decomposer.decompose(plan)
            logger.info(f"BuildPlan: complexity={build_plan.complexity}, steps={len(build_plan.steps)}")

            if build_plan.complexity != "simple" and len(build_plan.steps) > 1:
                if on_step:
                    await _call_step(on_step, StepUpdate(
                        step="generating_code",
                        message=f"\u590d\u6742\u96f6\u4ef6\uff0c\u5206 {len(build_plan.steps)} \u6b65\u6784\u5efa...",
                    ))
                multi_executor = MultiStepExecutor(self.code_gen, self.executor)
                result = await multi_executor.execute_plan(
                    build_plan, plan, examples, on_step
                )

                # Auto-DFM for complex parts too
                if result.success and self._should_auto_dfm(prompt):
                    try:
                        if on_step:
                            await _call_step(on_step, StepUpdate(
                                step="dfm_analysis", message="\u6b63\u5728\u8fdb\u884c DFM \u53ef\u5236\u9020\u6027\u5206\u6790..."
                            ))
                        process_hint, material_hint = self._detect_process_material(prompt)
                        if profile:
                            process_hint = profile.process
                            material_hint = profile.material
                        dfm_data = await self._run_auto_dfm(
                            result.request_id, result.code or "", prompt,
                            process_hint, material_hint,
                        )
                        if dfm_data:
                            result.dfm_analysis = dfm_data
                            self._enrich_inspect_with_dfm(result, dfm_data)
                            score = dfm_data.get("design_score", 0)
                            if on_step:
                                await _call_step(on_step, StepUpdate(
                                    step="dfm_complete",
                                    message=f"DFM \u5206\u6790\u5b8c\u6210 (\u8bc4\u5206: {score}/100)",
                                ))
                    except Exception as e:
                        logger.warning(f"Auto-DFM skipped for multi-step: {e}")

                result.plan = plan  # surface the requirement brief (A2)
                return result

            # Simple part — single-step generation
            if on_step:
                await _call_step(on_step, StepUpdate(step="generating_code", message="\u6b63\u5728\u751f\u6210 CadQuery \u4ee3\u7801..."))
            parts_info = self._lookup_standard_parts(plan)
            code = await self.code_gen.generate(
                plan, examples, [{"role": "user", "content": prompt}],
                extra_context=parts_info,
            )

        # Build assembly_parts metadata (only for assemblies)
        _assy_parts_info = None
        if is_assembly and part_codes:
            _assy_parts_info = [
                AssemblyPartInfo(
                    name=p["name"],
                    description=p.get("description", ""),
                    code=p["code"],
                    status=p.get("status", "success"),
                    position=p.get("position", [0, 0, 0]),
                    color=p.get("color", "lightgray"),
                )
                for p in part_codes
            ]

        # Step 4: Execute with retry loop + validation
        result = await self._execute_with_retry(
            request_id, code, plan, output_formats, on_step, prompt, is_2d=is_2d
        )

        # Surface the understood requirement brief (A2)
        result.plan = plan
        result.manufacturing_profile = profile

        # Attach assembly_parts to the result
        if _assy_parts_info:
            result.assembly_parts = _assy_parts_info

        # Populate the code cache on success (skip assemblies — richer multi-part state).
        if result.success and result.code and not is_assembly and cache:
            cache.put(cache_key, result.code)

        # Step 5: Auto-DFM analysis (when process/material keywords detected)
        if result.success and not is_2d and self._should_auto_dfm(prompt):
            try:
                if on_step:
                    await _call_step(on_step, StepUpdate(
                        step="dfm_analysis", message="\u6b63\u5728\u8fdb\u884c DFM \u53ef\u5236\u9020\u6027\u5206\u6790..."
                    ))

                process_hint, material_hint = self._detect_process_material(prompt)
                if profile:
                    process_hint = profile.process
                    material_hint = profile.material
                dfm_data = await self._run_auto_dfm(
                    request_id, result.code or "", prompt,
                    process_hint, material_hint,
                )
                if dfm_data:
                    result.dfm_analysis = dfm_data
                    self._enrich_inspect_with_dfm(result, dfm_data)
                    score = dfm_data.get("design_score", 0)
                    if on_step:
                        await _call_step(on_step, StepUpdate(
                            step="dfm_complete",
                            message=f"DFM \u5206\u6790\u5b8c\u6210 (\u8bc4\u5206: {score}/100)",
                        ))
            except Exception as e:
                logger.warning(f"Auto-DFM skipped: {e}")

        # Step 6: Strategy fallback — if failed, try alternative modeling approach
        if not result.success and not is_2d and not is_assembly:
            alt_hint = self._get_fallback_hint(plan.modeling_hint, result.error)
            if alt_hint:
                logger.info(f"Strategy fallback: {plan.modeling_hint} → {alt_hint}")
                if on_step:
                    await _call_step(on_step, StepUpdate(
                        step="generating_code",
                        message=f"\u6362\u7528 {alt_hint} \u7b56\u7565\u91cd\u65b0\u751f\u6210...",
                    ))
                plan.modeling_hint = alt_hint
                fallback_code = await self.code_gen.generate(
                    plan, examples, [{"role": "user", "content": prompt}],
                    extra_context=self._lookup_standard_parts(plan),
                )
                fallback_result = await self._execute_with_retry(
                    str(uuid.uuid4()), fallback_code, plan, output_formats,
                    on_step, prompt, is_2d=is_2d,
                )
                if fallback_result.success:
                    fallback_result.plan = plan  # surface the requirement brief (A2)
                    fallback_result.recovery_actions = build_recovery_actions(fallback_result)
                    return fallback_result

        result.recovery_actions = build_recovery_actions(result)
        return result

    async def modify(
        self,
        code: str,
        prompt: str,
        output_formats: list[str] = None,
        on_step=None,
    ) -> GenerateResponse:
        if output_formats is None:
            output_formats = ["step", "stl"]

        request_id = str(uuid.uuid4())
        set_request_id(request_id)

        if on_step:
            await _call_step(on_step, StepUpdate(step="planning", message="\u6b63\u5728\u5206\u6790\u4fee\u6539\u9700\u6c42..."))

        plan = await self.planner.plan_modification(
            [{"role": "user", "content": prompt}], code
        )

        examples = await self.retriever.find_similar(prompt, top_k=3)

        if on_step:
            await _call_step(on_step, StepUpdate(step="generating_code", message="\u6b63\u5728\u4fee\u6539\u4ee3\u7801..."))

        new_code = await self.code_gen.modify(
            plan, code, examples, [{"role": "user", "content": prompt}]
        )

        # Detect if original code is 2D (ezdxf) or 3D (CadQuery)
        is_2d = "ezdxf" in code or "result.dxf" in code

        response = await self._execute_with_retry(
            request_id, new_code, None, output_formats, on_step, prompt, is_2d=is_2d
        )
        response.recovery_actions = build_recovery_actions(response)
        return response

    async def execute_code(
        self,
        code: str,
        output_formats: list[str] = None,
    ) -> GenerateResponse:
        if output_formats is None:
            output_formats = ["step", "stl"]

        request_id = str(uuid.uuid4())
        set_request_id(request_id)

        # Detect 2D vs 3D from code content
        is_2d = "ezdxf" in code or "result.dxf" in code

        # Validate code
        is_valid, error_msg = validate_code(code)
        if not is_valid:
            response = GenerateResponse(
                request_id=request_id,
                success=False,
                error={"type": "ValidationError", "message": error_msg},
            )
            response.recovery_actions = build_recovery_actions(response)
            return response

        # Execute directly — no LLM
        exec_mode = "2d" if is_2d else "3d"
        result = await self.executor.execute(code, mode=exec_mode)
        try:
            if result.success:
                files = self._copy_output_files(
                    result.work_dir, request_id, output_formats
                )
                params = self._extract_params(code)

                # Run the same printability gate as /api/generate so parameter edits
                # don't silently produce an un-printable model (3D only; 2D has no STL).
                validation_data = None
                inspect_report = None
                if not is_2d:
                    stl_path = self._find_stl_in_output(result.work_dir)
                    if stl_path:
                        try:
                            geo = await self.geometry_validator.validate(stl_path)
                            validation_data = ValidationResult(
                                is_watertight=geo.is_watertight,
                                bounding_box=BoundingBox(**geo.bounding_box),
                                volume=geo.volume,
                                printable=geo.printable,
                                fits_build_volume=geo.fits_build_volume,
                                min_wall_thickness=geo.min_wall_thickness,
                                print_warnings=geo.print_warnings,
                            )
                            inspect_report = build_inspect_report(
                                geo,
                                available_exports=sorted(files.keys()),
                                repair_attempts=0,
                                source="geometry_validator",
                            )
                        except Exception as e:
                            logger.warning(f"execute_code geometry validation skipped: {e}")

                response = GenerateResponse(
                    request_id=request_id,
                    success=True,
                    files=files,
                    code=code,
                    params=params if params else None,
                    execution_time_ms=result.execution_time_ms,
                    attempts=1,
                    validation=validation_data,
                    inspect_report=inspect_report,
                )
                response.recovery_actions = build_recovery_actions(response)
                return response
            else:
                response = GenerateResponse(
                    request_id=request_id,
                    success=False,
                    code=code,
                    error={
                        "type": result.error_type or "ExecutionError",
                        "message": result.error_message or "Unknown error",
                    },
                    execution_time_ms=result.execution_time_ms,
                    attempts=1,
                )
                response.recovery_actions = build_recovery_actions(response)
                return response
        finally:
            shutil.rmtree(result.work_dir, ignore_errors=True)

    # === Stateful WebSocket method (Web frontend streaming) ===

    def _prompt_with_pending_design_brief(
        self, context: ConversationContext, user_message: str
    ) -> str:
        brief = context.current_design_brief
        if not brief or not brief.open_questions or context.current_code:
            return user_message
        questions = "\n".join(f"- {question}" for question in brief.open_questions)
        return (
            "Continue from this pending engineering brief before CAD generation.\n"
            f"Intent: {brief.intent_summary}\n"
            f"Artifact type: {brief.artifact_type}\n"
            f"Open questions:\n{questions}\n"
            f"User clarification: {user_message}"
        )

    async def handle_message(
        self,
        context: ConversationContext,
        user_message: str,
        on_step=None,
        manufacturing_profile: ManufacturingProfile | dict | None = None,
    ) -> GenerationResult:
        context.messages.append({"role": "user", "content": user_message})

        intent = self._detect_intent(user_message, context)
        logger.info(f"Intent detected: {intent}")

        if intent == "modify" and context.current_code:
            response = await self.modify(
                context.current_code, user_message, on_step=on_step
            )
        else:
            # generate or generate_relative both go through generate
            generation_prompt = self._prompt_with_pending_design_brief(context, user_message)
            response = await self.generate(
                generation_prompt,
                on_step=on_step,
                manufacturing_profile=manufacturing_profile or getattr(context, "current_manufacturing_profile", None),
            )

        # Update context
        if response.manufacturing_profile:
            context.current_manufacturing_profile = response.manufacturing_profile
        if response.needs_confirmation:
            context.current_design_brief = response.design_brief
        if response.success:
            context.current_code = response.code
            context.current_params = (
                {k: v.model_dump() for k, v in response.params.items()}
                if response.params
                else None
            )
            context.generation_count += 1
            context.current_design_brief = response.design_brief
            if response.assembly_parts:
                context.assembly_parts = [p.model_dump() for p in response.assembly_parts]
            else:
                context.assembly_parts = None

        if on_step and not response.needs_confirmation:
            step = "complete" if response.success else "failed"
            msg = "\u751f\u6210\u5b8c\u6210" if response.success else f"\u751f\u6210\u5931\u8d25: {response.error}"
            await _call_step(on_step, StepUpdate(step=step, message=msg))

        return GenerationResult(
            success=response.success,
            needs_confirmation=response.needs_confirmation,
            manufacturing_profile=response.manufacturing_profile,
            request_id=response.request_id,
            files=response.files,
            code=response.code,
            params=response.params,
            execution_time_ms=response.execution_time_ms,
            attempts=response.attempts,
            error=response.error,
            validation=response.validation,
            assembly_parts=response.assembly_parts,
            inspect_report=response.inspect_report,
            plan=response.plan,
            design_brief=response.design_brief,
        )

    def _detect_intent(self, message: str, context: ConversationContext) -> str:
        """Detect user intent: generate / modify / generate_relative."""
        has_code = context.current_code is not None
        has_selection = "[Selection Context]" in message

        # Explicit modification keywords
        modify_keywords = [
            "\u6539", "\u4fee\u6539", "\u8c03\u6574", "\u4f18\u5316", "\u52a0", "\u589e\u52a0", "\u6dfb\u52a0",
            "\u5220", "\u5220\u9664", "\u79fb\u9664", "\u53d8", "\u53d8\u6210", "\u7f29\u5c0f", "\u653e\u5927",
            "\u52a0\u539a", "\u53d8\u8584", "\u5012\u89d2", "\u5706\u89d2", "\u5f00\u5b54", "\u6253\u5b54",
            "\u79fb\u52a8", "\u65cb\u8f6c", "\u66ff\u6362", "\u4fee\u590d", "\u589e\u5f3a", "\u51cf\u5c0f",
        ]

        # Generate relative to selection
        relative_keywords = ["\u57fa\u4e8e", "\u53c2\u8003", "\u6cbf\u7740", "\u56f4\u7ed5", "\u8fd9\u4e2a", "\u5f53\u524d", "\u9009\u4e2d\u90e8\u5206"]

        # Explicit new generation
        generate_keywords = ["\u8bbe\u8ba1", "\u751f\u6210", "\u521b\u5efa", "\u5efa\u6a21", "\u505a\u4e00\u4e2a", "\u5236\u4f5c", "\u91cd\u65b0\u751f\u6210"]

        # Strong "fresh start" markers: even with existing code + a modify-ish word,
        # these mean the user wants a NEW model, not an edit of the current one.
        fresh_start_keywords = ["\u91cd\u65b0\u751f\u6210", "\u91cd\u65b0\u8bbe\u8ba1", "\u65b0\u5efa\u4e00\u4e2a", "\u53e6\u505a\u4e00\u4e2a", "\u4ece\u5934\u751f\u6210", "\u6362\u4e00\u4e2a", "\u505a\u4e00\u4e2a\u65b0\u7684"]
        if any(kw in message for kw in fresh_start_keywords):
            return "generate"

        # Check for modification intent
        if has_code and any(kw in message for kw in modify_keywords):
            return "modify"

        # With existing code, a selection normally means editing the current model.
        if has_code and has_selection:
            return "modify"

        # Without existing code, selection context should generate from the selection.
        if has_selection and (any(kw in message for kw in relative_keywords) or not has_code):
            return "generate_relative"

        # Explicit generation
        if any(kw in message for kw in generate_keywords):
            return "generate"

        # Has code + selection → likely modification
        if has_code and has_selection:
            return "modify"

        # Default
        return "generate"

    async def modify_assembly_part(
        self,
        context: ConversationContext,
        part_name: str,
        instruction: str,
        on_step=None,
    ) -> GenerationResult:
        """Modify a single part within an assembly and rebuild."""
        if not context.assembly_parts:
            return GenerationResult(
                success=False,
                error={"type": "ValidationError", "message": "\u5f53\u524d\u4ee3\u7801\u4e0d\u5305\u542b\u88c5\u914d\u4f53\u96f6\u4ef6"},
            )

        # Find the target part
        target_idx = None
        for i, p in enumerate(context.assembly_parts):
            if p["name"] == part_name:
                target_idx = i
                break

        if target_idx is None:
            return GenerationResult(
                success=False,
                error={"type": "ValidationError", "message": f"未找到零件: {part_name}"},
            )

        target_part = context.assembly_parts[target_idx]

        if on_step:
            await _call_step(on_step, StepUpdate(
                step="generating_code",
                message=f"\u6b63\u5728\u4fee\u6539\u96f6\u4ef6: {part_name}...",
                part_name=part_name,
                part_index=target_idx,
                total_parts=len(context.assembly_parts),
            ))

        # Use code_gen.modify to modify the part code
        plan = await self.planner.plan_modification(
            [{"role": "user", "content": instruction}], target_part["code"]
        )
        examples = await self.retriever.find_similar(instruction, top_k=2)
        new_part_code = await self.code_gen.modify(
            plan, target_part["code"], examples,
            [{"role": "user", "content": instruction}],
        )

        # Validate the new part code
        part_result = await self.executor.execute(new_part_code)
        if not part_result.success:
            if on_step:
                await _call_step(on_step, StepUpdate(
                    step="fixing_error",
                    message=f"\u96f6\u4ef6 {part_name} \u4fee\u6539\u540e\u6267\u884c\u5931\u8d25\uff0c\u5c1d\u8bd5\u4fee\u590d...",
                    part_name=part_name,
                ))
            shutil.rmtree(part_result.work_dir, ignore_errors=True)
            new_part_code = await self.code_gen.fix_error(
                new_part_code,
                {
                    "type": part_result.error_type or "ExecutionError",
                    "message": part_result.error_message or "",
                    "traceback": part_result.traceback or "",
                },
                None,
            )
            retry_result = await self.executor.execute(new_part_code)
            shutil.rmtree(retry_result.work_dir, ignore_errors=True)
            if not retry_result.success:
                return GenerationResult(
                    success=False,
                    error={"type": "ExecutionError", "message": f"\u96f6\u4ef6 {part_name} \u4fee\u6539\u5931\u8d25: {retry_result.error_message}"},
                )
        else:
            shutil.rmtree(part_result.work_dir, ignore_errors=True)

        # Update the part code in context
        clean_code = "\n".join(
            line for line in new_part_code.splitlines()
            if not line.strip().startswith("show_object")
        )
        context.assembly_parts[target_idx]["code"] = clean_code
        context.assembly_parts[target_idx]["status"] = "success"

        # Rebuild the assembly
        if on_step:
            await _call_step(on_step, StepUpdate(
                step="generating_code", message="\u6b63\u5728\u91cd\u65b0\u7ec4\u5408\u88c5\u914d\u4f53..."
            ))

        combined_code = await self.code_gen.generate_assembly_combiner(context.assembly_parts)

        request_id = str(uuid.uuid4())
        result = await self._execute_with_retry(
            request_id, combined_code, None, ["step", "stl"], on_step, instruction
        )

        # Attach updated assembly_parts
        assy_parts = [
            AssemblyPartInfo(**p) for p in context.assembly_parts
        ]
        result.assembly_parts = assy_parts

        if result.success:
            context.current_code = result.code

        if on_step:
            step = "complete" if result.success else "failed"
            msg = f"Part {part_name} modification complete" if result.success else "Assembly rebuild failed"
            await _call_step(on_step, StepUpdate(step=step, message=msg))

        return GenerationResult(
            success=result.success,
            request_id=result.request_id,
            files=result.files,
            code=result.code,
            params=result.params,
            execution_time_ms=result.execution_time_ms,
            attempts=result.attempts,
            error=result.error,
            validation=result.validation,
            assembly_parts=assy_parts,
            design_brief=context.current_design_brief,
        )

    # === Internal methods ===

    async def _execute_with_retry(
        self, request_id, code, plan, output_formats, on_step, user_prompt="", is_2d=False
    ) -> GenerateResponse:
        vision_retry_count = 0
        result = None
        # Oscillation guard: if fix_error keeps returning the same code, or the same
        # error type repeats, retrying just burns LLM calls without converging.
        last_error_sig: str | None = None
        repeat_error_count = 0
        repair_history: list[RepairStep] = []

        for attempt in range(1, self.MAX_RETRIES + 1):
            # Validate code (import whitelist)
            is_valid, error_msg = validate_code(code)
            if not is_valid:
                if attempt < self.MAX_RETRIES:
                    if on_step:
                        await _call_step(
                            on_step,
                            StepUpdate(
                                step="fixing_error",
                                message=f"\u4ee3\u7801\u6821\u9a8c\u5931\u8d25\uff0c\u6b63\u5728\u4fee\u590d... (\u5c1d\u8bd5 {attempt}/{self.MAX_RETRIES})",
                            ),
                        )
                    repair_history.append(RepairStep(
                        attempt=attempt,
                        stage="validation",
                        error_type="ValidationError",
                        message=error_msg or "Code validation failed",
                        action="fix_error",
                        status="repaired",
                    ))
                    code = await self.code_gen.fix_error(
                        code,
                        {"type": "ValidationError", "message": error_msg, "gate": "ValidationError"},
                        plan,
                    )
                    continue
                return GenerateResponse(
                    request_id=request_id,
                    success=False,
                    code=code,
                    error={"type": "ValidationError", "message": error_msg},
                    attempts=attempt,
                    repair_history=repair_history,
                    design_brief=plan.design_brief if plan else None,
                )

            # Static analysis — catch CadQuery anti-patterns before sandbox
            analysis_warnings = analyze_code(code)
            if analysis_warnings and attempt < self.MAX_RETRIES:
                logger.info(f"Static analysis warnings: {analysis_warnings}")
                if on_step:
                    await _call_step(
                        on_step,
                        StepUpdate(
                            step="fixing_error",
                            message="\u9759\u6001\u5206\u6790\u53d1\u73b0\u95ee\u9898\uff0c\u6b63\u5728\u4fee\u590d...",
                        ),
                    )
                repair_history.append(RepairStep(
                    attempt=attempt,
                    stage="static_analysis",
                    error_type="StaticAnalysis",
                    message="; ".join(analysis_warnings),
                    action="fix_error",
                    status="repaired",
                ))
                code = await self.code_gen.fix_error(
                    code,
                    {
                        "type": "StaticAnalysis",
                        "message": "; ".join(analysis_warnings),
                        "gate": "StaticAnalysis",
                    },
                    plan,
                )
                continue

            # Execute
            if on_step:
                await _call_step(
                    on_step,
                    StepUpdate(step="executing", message=f"\u6b63\u5728\u6267\u884c\u4ee3\u7801... (\u5c1d\u8bd5 {attempt}/{self.MAX_RETRIES})"),
                )

            exec_mode = "2d" if is_2d else "3d"
            result = await self.executor.execute(code, mode=exec_mode)

            if result.success:
                files = self._copy_output_files(
                    result.work_dir, request_id, output_formats
                )
                params = self._extract_params(code)

                # === 2D: ensure DXF in files + generate SVG preview ===
                if is_2d:
                    dxf_path = self._find_file_in_output(result.work_dir, ".dxf")
                    if dxf_path:
                        # Ensure DXF is in files dict (copy if not already)
                        if "dxf" not in files:
                            dest_dir = Path(settings.file_storage_dir) / request_id
                            dest_dir.mkdir(parents=True, exist_ok=True)
                            dest_dxf = dest_dir / dxf_path.name
                            shutil.copy2(dxf_path, dest_dxf)
                            files["dxf"] = f"/api/files/{request_id}/{dxf_path.name}"

                        try:
                            from app.rendering.dxf_renderer import dxf_to_svg
                            svg_dir = Path(settings.file_storage_dir) / request_id
                            svg_dir.mkdir(parents=True, exist_ok=True)
                            svg_path = svg_dir / "result.svg"
                            dxf_to_svg(dxf_path, svg_path)
                            files["svg"] = f"/api/files/{request_id}/result.svg"
                        except Exception as e:
                            logger.warning(f"SVG generation failed: {e}")

                    shutil.rmtree(result.work_dir, ignore_errors=True)
                    return GenerateResponse(
                        request_id=request_id,
                        success=True,
                        files=files,
                        code=code,
                        params=params if params else None,
                        execution_time_ms=result.execution_time_ms,
                        attempts=attempt,
                    design_brief=plan.design_brief if plan else None,
                        repair_history=repair_history,
                    )

                # === Geometry validation ===
                validation_data = None
                inspect_report = None
                stl_path = self._find_stl_in_output(result.work_dir)
                if stl_path:
                    try:
                        expected_dims = plan.dimensions if plan else None
                        geo_validation = await self.geometry_validator.validate(
                            stl_path, expected_dims
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
                        # Evidence inspect report — aggregate the facts just computed
                        # (built on every successful 3D gen; DFM enrichment added later).
                        inspect_report = build_inspect_report(
                            geo_validation,
                            available_exports=sorted(files.keys()),
                            repair_attempts=len(repair_history),
                            source="geometry_validator",
                        )

                        if not geo_validation.passed and attempt < self.MAX_RETRIES:
                            error_messages = "; ".join(
                                r.message for r in geo_validation.rules if not r.passed
                            )
                            shutil.rmtree(result.work_dir, ignore_errors=True)
                            if on_step:
                                await _call_step(
                                    on_step,
                                    StepUpdate(step="fixing_error", message=f"\u51e0\u4f55\u9a8c\u8bc1\u5931\u8d25: {error_messages}"),
                                )
                            repair_history.append(RepairStep(
                                attempt=attempt,
                                stage="geometry",
                                error_type="GeometryError",
                                message=error_messages,
                                action="fix_error",
                                status="repaired",
                            ))
                            code = await self.code_gen.fix_error(
                                code,
                                {"type": "GeometryError", "message": error_messages, "gate": "GeometryError"},
                                plan,
                            )
                            continue

                        # === Vision validation (max 2 retries) ===
                        if vision_retry_count < 2 and stl_path:
                            try:
                                if on_step:
                                    await _call_step(
                                        on_step,
                                        StepUpdate(step="executing", message="\u6b63\u5728\u8fdb\u884c\u89c6\u89c9\u6821\u9a8c..."),
                                    )
                                renders_dir = result.work_dir / "renders"
                                render_paths = self.renderer.render_stl(stl_path, renders_dir)
                                if not render_paths:
                                    # No renders → vision is INDETERMINATE, not skipped silently.
                                    # Surface it honestly instead of letting it pass invisibly.
                                    logger.warning("No render images produced, vision validation indeterminate")
                                    self._add_indeterminate_vision_check(
                                        inspect_report, "没有生成渲染图，视觉校验未执行"
                                    )
                                else:
                                    vision_result = await self.vision_validator.validate(
                                        user_prompt, render_paths, code
                                    )
                                    # Only an EXPLICIT mismatch (is_match is False) triggers a fix.
                                    # Indeterminate (None) never retries and never passes silently.
                                    if vision_result.is_match is False and attempt < self.MAX_RETRIES:
                                        vision_retry_count += 1
                                        shutil.rmtree(result.work_dir, ignore_errors=True)
                                        if on_step:
                                            await _call_step(
                                                on_step,
                                                StepUpdate(step="fixing_error", message="\u89c6\u89c9\u6821\u9a8c\u4e0d\u901a\u8fc7\uff0c\u6b63\u5728\u4fee\u590d..."),
                                            )
                                        repair_history.append(RepairStep(
                                            attempt=attempt,
                                            stage="vision",
                                            error_type="VisionMismatch",
                                            message="; ".join(vision_result.issues),
                                            action="fix_visual_issues",
                                            status="repaired",
                                        ))
                                        code = await self.code_gen.fix_visual_issues(
                                            code, vision_result.issues, vision_result.suggestions,
                                            on_step=on_step,
                                        )
                                        continue
                                    if vision_result.is_match is None:
                                        self._add_indeterminate_vision_check(
                                            inspect_report,
                                            "; ".join(vision_result.issues) or "视觉校验结果不确定",
                                        )
                            except Exception as e:
                                logger.warning(f"Vision validation skipped: {e}")
                                self._add_indeterminate_vision_check(
                                    inspect_report, "视觉校验发生异常，未执行"
                                )

                    except Exception as e:
                        logger.warning(f"Geometry validation skipped: {e}")

                shutil.rmtree(result.work_dir, ignore_errors=True)
                return GenerateResponse(
                    request_id=request_id,
                    success=True,
                    files=files,
                    code=code,
                    params=params if params else None,
                    execution_time_ms=result.execution_time_ms,
                    attempts=attempt,
                    validation=validation_data,
                    design_brief=plan.design_brief if plan else None,
                    inspect_report=inspect_report,
                    repair_history=repair_history,
                )

            # Failed — classify the failure to decide how (or whether) to retry.
            shutil.rmtree(result.work_dir, ignore_errors=True)
            fc = classify(
                result.error_type, result.error_message, result.traceback, gate="exec"
            )

            # HARD_STOP: infra failures (Docker down / image missing / no output) can't be
            # fixed by re-prompting the LLM — abort immediately instead of burning retries.
            if fc.fix_path is FixPath.HARD_STOP:
                logger.info(f"Non-recoverable failure ({fc.key}), stopping retries")
                break

            # Oscillation guard: normalize on the FAILURE CLASS (not the raw message tail),
            # so the same OCCT error with varying coordinates is recognized as a repeat.
            error_sig = fc.key
            if error_sig == last_error_sig:
                repeat_error_count += 1
            else:
                repeat_error_count = 0
                last_error_sig = error_sig
            if repeat_error_count >= 2:
                logger.info(f"Retry oscillation detected ({error_sig} 脳{repeat_error_count+1}), stopping early")
                break

            if attempt < self.MAX_RETRIES:
                if on_step:
                    await _call_step(
                        on_step,
                        StepUpdate(
                            step="fixing_error",
                            message=f"\u6267\u884c\u51fa\u9519\uff0c\u6b63\u5728\u4fee\u590d... (\u5c1d\u8bd5 {attempt}/{self.MAX_RETRIES})",
                        ),
                    )
                repair_history.append(RepairStep(
                    attempt=attempt,
                    stage="execution",
                    error_type=result.error_type or "ExecutionError",
                    message=result.error_message or "Execution failed",
                    action="fix_error",
                    status="repaired",
                ))
                prev_code = code
                code = await self.code_gen.fix_error(
                    code,
                    {
                        "type": result.error_type,
                        "message": result.error_message,
                        "traceback": result.traceback,
                        "gate": "exec",
                    },
                    plan,
                )
                # If the fixer returned identical code, further retries can't help.
                if code.strip() == prev_code.strip():
                    logger.info("fix_error returned identical code, stopping early")
                    break

        # All retries exhausted
        return GenerateResponse(
            request_id=request_id,
            success=False,
            code=code,
            error={
                "type": result.error_type or "ExecutionError",
                "message": result.error_message or "Max retries exceeded",
            },
            execution_time_ms=result.execution_time_ms if result else 0,
            attempts=self.MAX_RETRIES,
            repair_history=repair_history,
            design_brief=plan.design_brief if plan else None,
        )

    # Fallback strategy map: current_hint → alternative to try
    _FALLBACK_MAP = {
        "revolve": "extrude_cut",      # 回转失败 → 拉伸切除
        "sweep": "extrude_cut",        # 扫掠失败 → 拉伸切除
        "loft": "extrude_cut",         # 放样失败 → 拉伸切除
        "extrude_cut": "revolve",      # 拉伸失败 → 尝试回转
        "boolean_combine": "extrude_cut",
    }

    def _get_fallback_hint(self, current_hint: str, error: dict | None) -> str | None:
        """Return an alternative modeling hint, or None if no fallback available."""
        if not current_hint:
            return "extrude_cut"
        return self._FALLBACK_MAP.get(current_hint)

    def _find_stl_in_output(self, work_dir: Path) -> Path | None:
        return self._find_file_in_output(work_dir, ".stl")

    def _find_file_in_output(self, work_dir: Path, ext: str) -> Path | None:
        from app.sandbox.output_files import find_file_in_output
        return find_file_in_output(work_dir, ext)

    def _lookup_standard_parts(self, plan) -> str:
        """Scan plan for standard part references (M3, 608, etc.) and return info text."""
        try:
            from app.parts_library.data import format_for_prompt
        except ImportError:
            return ""

        text = f"{plan.description} {' '.join(plan.features)}"
        pattern = re.compile(r'[Mm]\d+(?:\.\d+)?|608|6[02]\d{2}')
        matches = set(pattern.findall(text))

        parts_info = []
        for m in matches:
            info = format_for_prompt(m)
            if info:
                parts_info.append(info)

        if parts_info:
            return "\n\n## 标准件参数\n" + "\n".join(parts_info)
        return ""

    def _extract_params(self, code: str) -> dict[str, ParamConfig]:
        from app.sandbox.output_files import extract_params
        return extract_params(code)

    def _copy_output_files(
        self, work_dir: Path, request_id: str, output_formats: list[str]
    ) -> dict[str, str]:
        from app.sandbox.output_files import copy_output_files
        files = copy_output_files(work_dir, request_id, output_formats)
        if files:
            logger.info(f"Output files saved: {list(files.keys())}")
        else:
            logger.warning(f"No output files found in {work_dir / 'output'}")
        return files

    # === Auto-DFM helpers ===

    _PROCESS_KEYWORDS = {
        "sheet_metal": ["sheet metal", "\u94a3\u91d1", "\u6298\u5f2f", "\u51b2\u538b", "\u8584\u677f", "\u94a3\u91d1\u4ef6"],
        "CNC": ["CNC", "cnc", "\u673a\u52a0\u5de5", "\u94e3\u524a", "\u8f66\u524a"],
        "FDM": ["3D\u6253\u5370", "FDM", "fdm", "3d\u6253\u5370"],
        "SLA": ["\u6811\u8102", "SLA", "sla", "\u5149\u56fa\u5316"],
        "injection_mold": ["\u6ce8\u5851", "\u6ce8\u5c04\u6210\u578b", "\u6a21\u5177"],
        "die_casting": ["\u538b\u94f8", "die casting", "\u94dd\u538b\u94f8", "\u950c\u538b\u94f8", "\u94f8\u9020"],
    }

    _MATERIAL_KEYWORDS = {
        "mat_al_sheet": ["5052", "\u94dd\u677f", "\u94dd\u5408\u91d1\u677f"],
        "mat_al6061": ["6061"],
        "mat_al7075": ["7075"],
        "mat_ss304": ["304", "\u4e0d\u9508\u94a2"],
        "mat_steel_sheet": ["SPCC", "\u51b7\u8f67\u94a2", "\u94a2\u677f"],
        "mat_adc12": ["ADC12", "adc12"],
        "mat_a380": ["A380", "a380"],
        "mat_zamak3": ["Zamak", "zamak", "\u950c\u5408\u91d1"],
    }

    _DFM_TRIGGER_KEYWORDS = ["DFM", "dfm", "DFM\u68c0\u67e5", "\u53ef\u5236\u9020\u6027", "\u5236\u9020\u7ea6\u675f", "\u5de5\u827a\u68c0\u67e5"]

    def _detect_process_material(self, prompt: str) -> tuple[str | None, str | None]:
        process = None
        material = None
        for proc, keywords in self._PROCESS_KEYWORDS.items():
            if any(kw in prompt for kw in keywords):
                process = proc
                break
        for mat_id, keywords in self._MATERIAL_KEYWORDS.items():
            if any(kw in prompt for kw in keywords):
                material = mat_id
                break
        return process, material

    def _should_auto_dfm(self, prompt: str) -> bool:
        process, material = self._detect_process_material(prompt)
        if process or material:
            return True
        return any(kw in prompt for kw in self._DFM_TRIGGER_KEYWORDS)

    @staticmethod
    def _enrich_inspect_with_dfm(result: GenerateResponse, dfm_data: dict) -> None:
        """Fold DFM score + violations into the already-built inspect report (in place)."""
        if result.inspect_report is None:
            return
        from app.models.schemas import RuleViolationModel
        result.inspect_report.design_score = dfm_data.get("design_score")
        result.inspect_report.dfm_violations = [
            RuleViolationModel(**rv) for rv in dfm_data.get("rule_violations", [])
        ]

    @staticmethod
    def _add_indeterminate_vision_check(report, message: str) -> None:
        """Record an honest 'vision indeterminate' check (warn) on the inspect report.

        Indeterminate means the visual self-check could not run / be trusted — it must
        never read as a pass, so we surface it as a warning rather than hiding it."""
        if report is None:
            return
        from app.models.schemas import InspectCheck
        from app.validation.inspect import add_check
        add_check(report, InspectCheck(
            name="vision", status="warn", message=message, source="vision",
        ))

    async def _run_auto_dfm(
        self,
        request_id: str,
        code: str,
        description: str,
        process: str | None,
        material: str | None,
    ) -> dict | None:
        import asyncio

        storage_dir = Path(settings.file_storage_dir) / request_id
        stl_path = next(storage_dir.glob("*.stl"), None)
        if not stl_path:
            return None

        step_path = next(storage_dir.glob("*.step"), None) or next(storage_dir.glob("*.stp"), None)

        try:
            from app.validation.dfm_analyzer import DFMAnalyzer
            analyzer = DFMAnalyzer()
            analysis = await asyncio.wait_for(
                analyzer.analyze(
                    stl_path=stl_path,
                    code=code,
                    description=description,
                    process=process,
                    step_path=step_path,
                    material=material,
                ),
                timeout=30,
            )

            return {
                "design_score": analysis.design_score,
                "design_summary": analysis.design_summary,
                "structural_issues": analysis.structural_issues,
                "functional_notes": analysis.functional_notes,
                "recommended_process": analysis.recommended_process,
                "process_compatibility": analysis.process_compatibility,
                "dfm_issues": [
                    {
                        "category": i.category,
                        "severity": i.severity,
                        "description": i.description,
                        "suggestion": i.suggestion,
                        "location": i.location,
                    }
                    for i in analysis.dfm_issues
                ],
                "estimated_difficulty": analysis.estimated_difficulty,
                "rule_violations": analysis.rule_violations,
            }
        except asyncio.TimeoutError:
            logger.warning("Auto-DFM timed out (30s)")
            return None
        except Exception as e:
            logger.warning(f"Auto-DFM failed: {e}")
            return None


async def _call_step(on_step, step: StepUpdate):
    import asyncio

    result = on_step(ensure_timeline_fields(step))
    if asyncio.iscoroutine(result):
        await result
