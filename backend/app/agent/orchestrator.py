import asyncio
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.code_gen import CodeGenerator
from app.agent.failure_taxonomy import FixPath, classify
from app.agent.planner import Planner
from app.config import settings
from app.logging_context import set_request_id
from app.models.schemas import (
    AssemblyPartInfo,
    BoundingBox,
    GenerateResponse,
    GenerationResult,
    ParamConfig,
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


@dataclass
class ConversationContext:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    current_code: str | None = None
    current_params: dict | None = None
    generation_count: int = 0
    assembly_parts: list[dict] | None = None  # per-part code/metadata for assemblies


class Orchestrator:
    MAX_RETRIES = 5

    def __init__(self):
        self.planner = Planner()
        self.code_gen = CodeGenerator()
        self.executor = CadQueryExecutor()
        try:
            from app.examples.vector_retriever import VectorExampleRetriever
            self.retriever = VectorExampleRetriever()
            logger.info("Using VectorExampleRetriever (ChromaDB)")
        except Exception:
            from app.examples.retriever import ExampleRetriever
            self.retriever = ExampleRetriever()
            logger.info("Falling back to TF-IDF ExampleRetriever")
        self.renderer = CADRenderer()
        self.geometry_validator = GeometryValidator()
        self.vision_validator = VisionValidator()
        from app.agent.code_cache import CodeCache
        self.code_cache = CodeCache()

    # === Stateless REST methods (Scheme C core) ===

    async def generate(
        self,
        prompt: str,
        output_formats: list[str] = None,
        on_step=None,
    ) -> GenerateResponse:
        if output_formats is None:
            output_formats = ["step", "stl"]

        request_id = str(uuid.uuid4())
        set_request_id(request_id)

        # Cache hit: identical prompt already produced working code — skip the 2 LLM
        # calls (plan + codegen) and re-execute the cached code (fresh files/validation).
        cache = getattr(self, "code_cache", None)
        cached_code = cache.get(prompt) if cache else None
        if cached_code:
            logger.info("Code cache hit — skipping LLM plan+codegen")
            if on_step:
                await _call_step(on_step, StepUpdate(step="executing", message="命中缓存，正在执行..."))
            is_2d_cached = "ezdxf" in cached_code or "result.dxf" in cached_code
            cached_result = await self._execute_with_retry(
                request_id, cached_code, None, output_formats, on_step, prompt, is_2d=is_2d_cached
            )
            if cached_result.success:
                return cached_result
            # cache produced stale/broken code — fall through to a fresh generation
            logger.info("Cached code failed on re-execution, regenerating")

        # Step 1: Planning
        if on_step:
            await _call_step(on_step, StepUpdate(step="planning", message="正在理解你的需求... (LLM 规划中)"))

        plan = await self.planner.plan_new([{"role": "user", "content": prompt}])
        logger.info(f"Plan: type={plan.part_type}, hint={plan.modeling_hint}, dims={plan.dimensions}")

        # Step 2: Retrieve examples
        if on_step:
            await _call_step(on_step, StepUpdate(step="retrieving_examples", message="正在查找相似案例..."))

        examples = await self.retriever.find_similar(plan.description, top_k=3)

        # Step 3: Generate code
        is_2d = plan.part_type == "profile_2d"
        is_assembly = plan.part_type == "assembly"

        if is_2d:
            if on_step:
                await _call_step(on_step, StepUpdate(step="generating_code", message="正在生成 ezdxf 2D 代码..."))
            code = await self.code_gen.generate_2d(
                plan, examples, [{"role": "user", "content": prompt}]
            )
            output_formats = ["dxf"]
        elif is_assembly:
            if on_step:
                await _call_step(on_step, StepUpdate(step="planning", message="正在拆解装配体零件清单..."))

            from app.agent.assembly_planner import AssemblyPlanner
            assy_planner = AssemblyPlanner()
            assy_plan = await assy_planner.plan_assembly(plan)
            total_parts = len(assy_plan.parts)
            logger.info(f"Assembly plan: {total_parts} parts — {[p.name for p in assy_plan.parts]}")

            if on_step:
                await _call_step(on_step, StepUpdate(
                    step="generating_code",
                    message=f"装配体共 {total_parts} 个零件，并行生成中...",
                ))

            # Generate all parts in parallel (LLM calls), then execute sequentially
            import asyncio as _asyncio

            async def _generate_part(idx, apart):
                if on_step:
                    await _call_step(on_step, StepUpdate(
                        step="assembly_part",
                        message=f"正在生成零件 {idx+1}/{total_parts}: {apart.name}",
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
                            message=f"零件 {apart.name} 验证通过",
                            part_name=apart.name,
                            part_index=idx,
                            total_parts=total_parts,
                        ))
                    shutil.rmtree(part_result.work_dir, ignore_errors=True)
                else:
                    if on_step:
                        await _call_step(on_step, StepUpdate(
                            step="fixing_error",
                            message=f"零件 {apart.name} 执行失败，正在修复...",
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
                    message=f"警告: 以下零件生成失败: {', '.join(failed_parts)}",
                ))

            # Combine all parts into assembly
            if on_step:
                await _call_step(on_step, StepUpdate(
                    step="generating_code",
                    message="正在组合装配体...",
                ))
            code = await self.code_gen.generate_assembly_combiner(part_codes)
        else:
            # Multi-step decomposition for complex parts
            from app.agent.multi_step import PlanDecomposer, MultiStepExecutor
            if on_step:
                await _call_step(on_step, StepUpdate(step="generating_code", message="正在分析零件复杂度..."))
            decomposer = PlanDecomposer()
            build_plan = await decomposer.decompose(plan)
            logger.info(f"BuildPlan: complexity={build_plan.complexity}, steps={len(build_plan.steps)}")

            if build_plan.complexity != "simple" and len(build_plan.steps) > 1:
                if on_step:
                    await _call_step(on_step, StepUpdate(
                        step="generating_code",
                        message=f"复杂零件，分 {len(build_plan.steps)} 步构建...",
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
                                step="dfm_analysis", message="正在进行 DFM 可制造性分析..."
                            ))
                        process_hint, material_hint = self._detect_process_material(prompt)
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
                                    message=f"DFM 分析完成 (评分: {score}/100)",
                                ))
                    except Exception as e:
                        logger.warning(f"Auto-DFM skipped for multi-step: {e}")

                result.plan = plan  # surface the requirement brief (A2)
                return result

            # Simple part — single-step generation
            if on_step:
                await _call_step(on_step, StepUpdate(step="generating_code", message="正在生成 CadQuery 代码..."))
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

        # Attach assembly_parts to the result
        if _assy_parts_info:
            result.assembly_parts = _assy_parts_info

        # Populate the code cache on success (skip assemblies — richer multi-part state).
        # A result whose FINAL vision check failed is still returned (with an honest
        # fail check on the report) but must not poison the cache for future prompts.
        if (
            result.success and result.code and not is_assembly and cache
            and not self._vision_failed(result)
        ):
            cache.put(prompt, result.code)

        # Step 5: Auto-DFM analysis (when process/material keywords detected)
        if result.success and not is_2d and self._should_auto_dfm(prompt):
            try:
                if on_step:
                    await _call_step(on_step, StepUpdate(
                        step="dfm_analysis", message="正在进行 DFM 可制造性分析..."
                    ))

                process_hint, material_hint = self._detect_process_material(prompt)
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
                            message=f"DFM 分析完成 (评分: {score}/100)",
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
                        message=f"换用 {alt_hint} 策略重新生成...",
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
                    return fallback_result

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
            await _call_step(on_step, StepUpdate(step="planning", message="正在分析修改需求..."))

        plan = await self.planner.plan_modification(
            [{"role": "user", "content": prompt}], code
        )

        examples = await self.retriever.find_similar(prompt, top_k=3)

        if on_step:
            await _call_step(on_step, StepUpdate(step="generating_code", message="正在修改代码..."))

        new_code = await self.code_gen.modify(
            plan, code, examples, [{"role": "user", "content": prompt}]
        )

        # Detect if original code is 2D (ezdxf) or 3D (CadQuery)
        is_2d = "ezdxf" in code or "result.dxf" in code

        return await self._execute_with_retry(
            request_id, new_code, None, output_formats, on_step, prompt, is_2d=is_2d
        )

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
            return GenerateResponse(
                request_id=request_id,
                success=False,
                error={"type": "ValidationError", "message": error_msg},
            )

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
                        except Exception as e:
                            logger.warning(f"execute_code geometry validation skipped: {e}")

                return GenerateResponse(
                    request_id=request_id,
                    success=True,
                    files=files,
                    code=code,
                    params=params if params else None,
                    execution_time_ms=result.execution_time_ms,
                    attempts=1,
                    validation=validation_data,
                )
            else:
                return GenerateResponse(
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
        finally:
            shutil.rmtree(result.work_dir, ignore_errors=True)

    # === Stateful WebSocket method (Web frontend streaming) ===

    async def handle_message(
        self,
        context: ConversationContext,
        user_message: str,
        on_step=None,
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
            response = await self.generate(user_message, on_step=on_step)

        # Update context
        if response.success:
            context.current_code = response.code
            context.current_params = (
                {k: v.model_dump() for k, v in response.params.items()}
                if response.params
                else None
            )
            context.generation_count += 1
            if response.assembly_parts:
                context.assembly_parts = [p.model_dump() for p in response.assembly_parts]
            else:
                context.assembly_parts = None

        if on_step:
            step = "complete" if response.success else "failed"
            msg = "生成完成！" if response.success else f"生成失败: {response.error}"
            await _call_step(on_step, StepUpdate(step=step, message=msg))

        return GenerationResult(
            success=response.success,
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
        )

    def _detect_intent(self, message: str, context: ConversationContext) -> str:
        """Detect user intent: generate / modify / generate_relative."""
        has_code = context.current_code is not None
        has_selection = "[Selection Context]" in message

        # Explicit modification keywords
        modify_keywords = [
            "改", "修改", "调整", "增大", "减小", "移动", "删除",
            "加厚", "加高", "变大", "变小", "换成", "改为", "改成",
            "加一个", "去掉", "圆角改", "直径改", "高度改", "宽度改",
            "缩小", "放大", "旋转", "镜像", "倒角", "圆角",
        ]

        # Generate relative to selection
        relative_keywords = ["根据", "配套", "插入", "匹配", "适配", "基于此", "基于选中"]

        # Explicit new generation
        generate_keywords = ["生成", "创建", "画一个", "做一个", "新建", "新的", "设计一个"]

        # Strong "fresh start" markers: even with existing code + a modify-ish word,
        # these mean the user wants a NEW model, not an edit of the current one.
        fresh_start_keywords = ["重新生成", "重新设计", "重新画", "换一个", "另做一个", "另外做", "再做一个"]
        if any(kw in message for kw in fresh_start_keywords):
            return "generate"

        # Check for modification intent
        if has_code and any(kw in message for kw in modify_keywords):
            return "modify"

        # Check for generate relative to selection
        if has_selection and any(kw in message for kw in relative_keywords):
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
                error={"type": "ValidationError", "message": "当前不是装配体，无法修改零件"},
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
                message=f"正在修改零件: {part_name}...",
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
                    message=f"零件 {part_name} 修改后执行失败，尝试修复...",
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
                    error={"type": "ExecutionError", "message": f"零件 {part_name} 修改失败: {retry_result.error_message}"},
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
                step="generating_code", message="正在重新组合装配体..."
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
            msg = f"零件 {part_name} 修改完成！" if result.success else f"装配体重建失败"
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
                                message=f"代码校验失败，正在修复... (尝试 {attempt}/{self.MAX_RETRIES})",
                            ),
                        )
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
                            message=f"静态分析发现问题，正在修复...",
                        ),
                    )
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
                    StepUpdate(step="executing", message=f"正在执行代码... (尝试 {attempt}/{self.MAX_RETRIES})"),
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
                        inspect_report = build_inspect_report(geo_validation)

                        if not geo_validation.passed and attempt < self.MAX_RETRIES:
                            error_messages = "; ".join(
                                r.message for r in geo_validation.rules if not r.passed
                            )
                            shutil.rmtree(result.work_dir, ignore_errors=True)
                            if on_step:
                                await _call_step(
                                    on_step,
                                    StepUpdate(step="fixing_error", message=f"几何验证失败: {error_messages}"),
                                )
                            code = await self.code_gen.fix_error(
                                code,
                                {"type": "GeometryError", "message": error_messages, "gate": "GeometryError"},
                                plan,
                            )
                            continue

                        # === Vision validation (verify → critique → regenerate loop) ===
                        # Runs on EVERY successful attempt, including after the fix
                        # budget is spent — a final mismatch is then recorded as an
                        # honest FAIL check instead of silently passing.
                        if stl_path:
                            try:
                                if on_step:
                                    await _call_step(
                                        on_step,
                                        StepUpdate(step="executing", message="正在进行视觉校验..."),
                                    )
                                renders_dir = result.work_dir / "renders"
                                # Rendering is CPU-bound and synchronous (trimesh GL or
                                # the matplotlib software fallback) — run it off the event
                                # loop so a slow render can't stall the whole server.
                                render_paths = await asyncio.to_thread(
                                    self.renderer.render_stl, stl_path, renders_dir
                                )
                                if not render_paths:
                                    # No renders → vision is INDETERMINATE, not skipped silently.
                                    # Surface it honestly instead of letting it pass invisibly.
                                    logger.warning("No render images produced, vision validation indeterminate")
                                    self._add_indeterminate_vision_check(
                                        inspect_report, "无渲染图，视觉校验未执行"
                                    )
                                else:
                                    vision_result = await self.vision_validator.validate(
                                        user_prompt, render_paths, code
                                    )
                                    # Only an EXPLICIT mismatch (is_match is False) triggers a fix.
                                    # Indeterminate (None) never retries and never passes silently.
                                    if vision_result.is_match is False:
                                        if (
                                            vision_retry_count < settings.vision_max_retries
                                            and attempt < self.MAX_RETRIES
                                        ):
                                            vision_retry_count += 1
                                            shutil.rmtree(result.work_dir, ignore_errors=True)
                                            if on_step:
                                                await _call_step(
                                                    on_step,
                                                    StepUpdate(step="fixing_error", message="视觉校验不通过，正在修复..."),
                                                )
                                            code = await self.code_gen.fix_visual_issues(
                                                code, vision_result.issues, vision_result.suggestions,
                                                on_step=on_step,
                                            )
                                            continue
                                        # Fix budget exhausted and the artifact still
                                        # doesn't match — say so instead of hiding it.
                                        self._add_failed_vision_check(
                                            inspect_report,
                                            "; ".join(vision_result.issues) or "视觉校验不通过",
                                        )
                                    elif vision_result.is_match is None:
                                        self._add_indeterminate_vision_check(
                                            inspect_report,
                                            "; ".join(vision_result.issues) or "视觉校验结果不可信",
                                        )
                            except Exception as e:
                                logger.warning(f"Vision validation skipped: {e}")
                                self._add_indeterminate_vision_check(
                                    inspect_report, "视觉校验异常，未执行"
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
                    inspect_report=inspect_report,
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
                logger.info(f"Retry oscillation detected ({error_sig} ×{repeat_error_count+1}), stopping early")
                break

            if attempt < self.MAX_RETRIES:
                if on_step:
                    await _call_step(
                        on_step,
                        StepUpdate(
                            step="fixing_error",
                            message=f"执行出错，正在修复... (尝试 {attempt}/{self.MAX_RETRIES})",
                        ),
                    )
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
        )

    # Fallback strategy map: current_hint → alternative to try
    _FALLBACK_MAP = {
        "revolve": "extrude_cut",      # revolve 失败 → 拉伸+切割
        "sweep": "extrude_cut",        # sweep 失败 → 拉伸+切割
        "loft": "extrude_cut",         # loft 失败 → 拉伸+切割
        "extrude_cut": "revolve",      # 拉伸失败 → 试试回转
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
        "sheet_metal": ["钣金", "板金", "折弯", "sheet metal", "冲压", "激光切割板"],
        "CNC": ["CNC", "cnc", "铣削", "车削", "机加工"],
        "FDM": ["3D打印", "FDM", "fdm", "3d打印"],
        "SLA": ["光固化", "SLA", "sla", "树脂打印"],
        "injection_mold": ["注塑", "注射成型", "开模"],
        "die_casting": ["压铸", "die casting", "铝压铸", "锌压铸", "压铸铝"],
    }

    _MATERIAL_KEYWORDS = {
        "mat_al_sheet": ["5052", "铝板", "铝合金板"],
        "mat_al6061": ["6061"],
        "mat_al7075": ["7075"],
        "mat_ss304": ["304", "不锈钢"],
        "mat_steel_sheet": ["SPCC", "冷轧钢", "钢板"],
        "mat_adc12": ["ADC12", "adc12"],
        "mat_a380": ["A380", "a380"],
        "mat_zamak3": ["Zamak", "zamak", "锌合金"],
    }

    _DFM_TRIGGER_KEYWORDS = ["DFM", "dfm", "DFM检测", "可制造性", "制造性分析", "工艺检查"]

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
    def _add_failed_vision_check(report, message: str) -> None:
        """Record that the artifact still failed the visual check after the fix budget
        was spent. The result is returned (it may still be usable) but the report says
        FAIL and generate() refuses to cache the code."""
        if report is None:
            return
        from app.models.schemas import InspectCheck
        from app.validation.inspect import add_check
        add_check(report, InspectCheck(
            name="vision", status="fail", message=message, source="vision",
        ))

    @staticmethod
    def _vision_failed(result) -> bool:
        report = getattr(result, "inspect_report", None)
        if not report:
            return False
        return any(c.name == "vision" and c.status == "fail" for c in report.checks)

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

    result = on_step(step)
    if asyncio.iscoroutine(result):
        await result
