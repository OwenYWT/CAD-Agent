import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


from app.config import settings, make_llm_client
from app.agent.failure_taxonomy import FixPath, classify
from app.llm import find_provider_exception
from app.models.schemas import CADPlan, GenerateResponse, ParamConfig, StepUpdate

logger = logging.getLogger(__name__)


class BuildPhase(str, Enum):
    BASE = "base"
    PRIMARY = "primary"
    SECONDARY = "secondary"
    REFINEMENT = "refinement"


@dataclass
class BuildStep:
    phase: BuildPhase
    description: str
    code_snippet: str = ""
    success: bool = False
    error: str | None = None


@dataclass
class BuildPlan:
    steps: list[BuildStep]
    complexity: str  # "simple"|"moderate"|"complex"


DECOMPOSE_PROMPT = """你是 CAD 构建规划专家。将零件描述分解为有序构建步骤。

## 规则
1. 第一步总是 phase="base"，创建基础形体
2. 然后 phase="primary": 抽壳、主要通孔、凸台
3. 然后 phase="secondary": 安装孔、散热筋、减重槽、安装耳等（每种特征单独一步）
4. 最后 phase="refinement": 圆角、倒角
5. 简单零件 (≤2 特征) 返回 1-2 步，不要过度分解
6. 最多 10 步，复杂零件应充分分步，每步只做一种特征
7. 复杂工业零件 (减速器/齿轮箱/逆止器等): 用简化几何，不要尝试精确齿形/螺纹
8. 装配体: 每个零件用函数定义，最后用 cq.Assembly() 组装

## 关键要求
- description 中必须包含**精确的数值参数**（尺寸、位置、数量、间距等）
- 不要写模糊描述如"添加安装孔"，要写"在底面四角添加4个M3通孔(Φ3.4mm)，按120x60mm矩形阵列分布"
- 每步的 description 必须自包含，LLM 仅凭 description 就能写出正确代码
- 每步只聚焦一种特征，降低单步复杂度

## 输出 JSON (不要输出其他文字):
{
    "complexity": "complex",
    "steps": [
        {"phase": "base", "description": "创建 160x100x35mm 长方体，原点在底面中心"},
        {"phase": "primary", "description": "从顶面(>Z)抽壳，壁厚3mm，底部保留"},
        {"phase": "secondary", "description": "内部底面四角添加4个M3螺丝柱：外径6mm，内孔Φ2.5mm，高度15mm，位置按120x70mm矩形阵列"},
        {"phase": "secondary", "description": "外侧长边(>Y和<Y面)各添加4条散热筋：宽2mm，高3mm，间距20mm，从壳体外壁面凸出，共8条，用union合并到主体"},
        {"phase": "secondary", "description": "底部外表面添加蜂窝状减重槽：六边形阵列，六边形外接圆直径10mm，间距12mm，深度1.5mm，用cut从底面挖除"},
        {"phase": "secondary", "description": "左右两侧(<X和>X面)各添加2个安装耳：20x15x3mm凸台，从侧壁外表面延伸，中心有Φ5.5mm通孔，用union合并到主体"},
        {"phase": "refinement", "description": "所有外边缘倒角1mm"}
    ]
}"""


class PlanDecomposer:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from app.config import settings
            if not settings.has_llm_credentials:
                raise RuntimeError(settings.llm_credentials_error)
            self._client = make_llm_client()
        return self._client

    async def decompose(self, plan: CADPlan) -> BuildPlan:
        user_content = (
            f"零件描述: {plan.description}\n"
            f"类型: {plan.part_type}\n"
            f"尺寸: {plan.dimensions}\n"
            f"特征: {plan.features}\n"
        )

        try:
            t0 = time.time()
            logger.info("PlanDecomposer LLM call start")
            response = await self.client.chat.completions.create(
                model=settings.llm_model,
                max_tokens=1024,
                temperature=0.1,
                messages=[
                    {"role": "system", "content": DECOMPOSE_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            elapsed = time.time() - t0
            logger.info(f"PlanDecomposer LLM call done in {elapsed:.1f}s")
            text = response.choices[0].message.content.strip()
            # Strip markdown code fences
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            parsed = json.loads(text)
            steps = []
            for s in parsed.get("steps", []):
                phase_str = s.get("phase", "base")
                try:
                    phase = BuildPhase(phase_str)
                except ValueError:
                    phase = BuildPhase.BASE
                steps.append(BuildStep(phase=phase, description=s.get("description", "")))

            complexity = parsed.get("complexity", "moderate")
            if not steps:
                raise ValueError("No steps returned")

            return BuildPlan(steps=steps[:6], complexity=complexity)

        except Exception as e:
            if find_provider_exception(e) is not None:
                raise
            logger.warning(f"PlanDecomposer failed: {e}, using single-step fallback")
            return BuildPlan(
                steps=[BuildStep(phase=BuildPhase.BASE, description=plan.description)],
                complexity="simple",
            )


class MultiStepExecutor:
    """Executes a BuildPlan step-by-step, accumulating code."""

    def __init__(self, code_gen, executor):
        self.code_gen = code_gen
        self.executor = executor

    MAX_STEP_RETRIES = 3

    async def execute_plan(
        self, build_plan: BuildPlan, plan: CADPlan, examples: list[dict], on_step=None
    ) -> GenerateResponse:
        accumulated_code = "import cadquery as cq\nimport math\n\n"
        request_id = str(uuid.uuid4())
        total = len(build_plan.steps)
        failed_steps: list[str] = []
        terminal_failure: tuple[str, str] | None = None
        execution_attempts = 0

        for i, step in enumerate(build_plan.steps):
            if on_step:
                await _call_step(
                    on_step,
                    StepUpdate(
                        step="multi_step",
                        message=f"步骤 {i + 1}/{total}: {step.description}",
                    ),
                )

            step_succeeded = False
            last_error_type = "ExecutionError"
            last_error_msg = "Unknown error"
            last_traceback = ""
            test_code = ""

            for attempt in range(1, self.MAX_STEP_RETRIES + 1):
                if attempt == 1:
                    snippet = await self.code_gen.generate_step(
                        step.description, accumulated_code, i, total,
                        plan_context=self._build_step_context(plan, failed_steps),
                    )
                else:
                    if on_step:
                        await _call_step(on_step, StepUpdate(
                            step="fixing_error",
                            message=f"步骤 {i + 1} 修复中 (尝试 {attempt}/{self.MAX_STEP_RETRIES})",
                        ))
                    snippet = await self.code_gen.fix_error(
                        test_code,
                        {
                            "type": last_error_type,
                            "message": last_error_msg,
                            "traceback": last_traceback,
                        },
                        plan,
                    )
                    # fix_error returns full code, extract only new part
                    lines = snippet.strip().splitlines()
                    clean_lines = [l for l in lines if not l.strip().startswith("show_object")]
                    snippet = "\n".join(clean_lines)

                test_code = accumulated_code + "\n" + snippet + "\nshow_object(result)"
                result = await self.executor.execute(test_code)
                execution_attempts += 1

                try:
                    if result.success:
                        accumulated_code += "\n" + snippet
                        step.success = True
                        step.code_snippet = snippet
                        step_succeeded = True
                        break
                    else:
                        last_error_type = result.error_type
                        last_error_msg = result.error_message
                        last_traceback = result.traceback
                        failure = classify(
                            result.error_type,
                            result.error_message,
                            result.traceback,
                            gate="exec",
                        )
                        if failure.fix_path is FixPath.HARD_STOP:
                            terminal_failure = (
                                result.error_type or "ExecutionError",
                                result.error_message or "Execution failed",
                            )
                            break
                        if (
                            failure.retry_budget is not None
                            and attempt > failure.retry_budget
                        ):
                            break
                finally:
                    shutil.rmtree(result.work_dir, ignore_errors=True)

            if not step_succeeded:
                step.error = last_error_msg
                failed_steps.append(step.description)
                logger.warning(f"Step {i + 1} failed after {self.MAX_STEP_RETRIES} retries: {step.error}")
            if terminal_failure:
                break

        if terminal_failure:
            return GenerateResponse(
                request_id=request_id,
                success=False,
                code=accumulated_code,
                error={
                    "type": terminal_failure[0],
                    "message": terminal_failure[1],
                },
                attempts=execution_attempts,
            )

        # Final execution with show_object
        final_code = accumulated_code + "\nshow_object(result)"

        if on_step:
            await _call_step(on_step, StepUpdate(step="executing", message="正在执行最终代码..."))

        final_result = await self.executor.execute(final_code)
        execution_attempts += 1

        try:
            if final_result.success:
                files = self._copy_output_files(
                    final_result.work_dir, request_id, ["step", "stl"]
                )
                params = self._extract_params(final_code)

                # Geometry validation
                validation_data = None
                geo_validator = None
                expected_dims = plan.dimensions if plan else None
                stl_path = self._find_file_in_output(final_result.work_dir, ".stl")
                if stl_path:
                    try:
                        from app.validation.geometry_validator import GeometryValidator
                        geo_validator = GeometryValidator()
                        geo_result = await geo_validator.validate(stl_path, expected_dims)

                        from app.models.schemas import ValidationResult, BoundingBox
                        validation_data = ValidationResult(
                            is_watertight=geo_result.is_watertight,
                            bounding_box=BoundingBox(**geo_result.bounding_box),
                            volume=geo_result.volume,
                        )

                        if on_step:
                            vol_str = f"{geo_result.volume:.0f}" if geo_result.volume else "?"
                            await _call_step(on_step, StepUpdate(
                                step="executing",
                                message=f"几何校验完成 (体积: {vol_str}mm³)",
                            ))
                    except Exception as e:
                        logger.warning(f"Multi-step geometry validation skipped: {e}")

                # Vision validation + auto-fix (max 3 rounds)
                if stl_path:
                    try:
                        from app.rendering.renderer import CADRenderer
                        from app.validation.vision_validator import VisionValidator

                        renderer = CADRenderer()
                        vision_validator = VisionValidator()
                        user_prompt = plan.description if plan else ""

                        for vision_round in range(2):
                            if on_step:
                                round_msg = f"正在进行视觉校验 ({vision_round + 1}/2)..." if vision_round > 0 else "正在进行视觉校验..."
                                await _call_step(on_step, StepUpdate(
                                    step="executing", message=round_msg
                                ))

                            renders_dir = final_result.work_dir / "renders"
                            if renders_dir.exists():
                                shutil.rmtree(renders_dir)
                            stl_path = self._find_file_in_output(final_result.work_dir, ".stl")
                            if not stl_path:
                                break
                            render_paths = renderer.render_stl(stl_path, renders_dir)
                            if not render_paths:
                                break

                            vision_result = await vision_validator.validate(
                                user_prompt, render_paths, final_code
                            )
                            # Stop on a confirmed match OR an indeterminate result (None);
                            # only an EXPLICIT mismatch (is_match is False) drives a fix round.
                            if vision_result.is_match is not False:
                                if on_step and vision_round > 0 and vision_result.is_match:
                                    await _call_step(on_step, StepUpdate(
                                        step="executing", message="视觉校验通过"
                                    ))
                                break

                            logger.info(f"Vision round {vision_round + 1} issues: {vision_result.issues}")
                            if on_step:
                                issues_str = "; ".join(vision_result.issues[:2])
                                await _call_step(on_step, StepUpdate(
                                    step="fixing_error",
                                    message=f"视觉校验不通过 ({vision_round + 1}/2)，正在修复: {issues_str}",
                                ))

                            fixed_code = await self.code_gen.fix_visual_issues(
                                final_code, vision_result.issues, vision_result.suggestions,
                                on_step=on_step,
                            )

                            shutil.rmtree(final_result.work_dir, ignore_errors=True)
                            retry_result = await self.executor.execute(fixed_code)

                            if retry_result.success:
                                final_code = fixed_code
                                final_result = retry_result
                                files = self._copy_output_files(
                                    final_result.work_dir, request_id, ["step", "stl"]
                                )
                                params = self._extract_params(final_code)

                                new_stl = self._find_file_in_output(final_result.work_dir, ".stl")
                                if new_stl and geo_validator:
                                    try:
                                        geo_result = await geo_validator.validate(new_stl, expected_dims)
                                        validation_data = ValidationResult(
                                            is_watertight=geo_result.is_watertight,
                                            bounding_box=BoundingBox(**geo_result.bounding_box),
                                            volume=geo_result.volume,
                                        )
                                    except Exception:
                                        pass
                            else:
                                logger.warning(f"Vision fix round {vision_round + 1} failed: {retry_result.error_message}")
                                shutil.rmtree(retry_result.work_dir, ignore_errors=True)
                                if on_step:
                                    await _call_step(on_step, StepUpdate(
                                        step="executing", message=f"视觉修复第 {vision_round + 1} 轮未成功"
                                    ))
                                break
                    except Exception as e:
                        logger.warning(f"Multi-step vision validation skipped: {e}")

                return GenerateResponse(
                    request_id=request_id,
                    success=True,
                    files=files,
                    code=final_code,
                    params=params if params else None,
                    execution_time_ms=final_result.execution_time_ms,
                    attempts=total,
                    validation=validation_data,
                )
            else:
                return GenerateResponse(
                    request_id=request_id,
                    success=False,
                    code=final_code,
                    error={
                        "type": final_result.error_type or "ExecutionError",
                        "message": final_result.error_message or "Final execution failed",
                    },
                    execution_time_ms=final_result.execution_time_ms,
                    attempts=total,
                )
        finally:
            shutil.rmtree(final_result.work_dir, ignore_errors=True)

    def _find_file_in_output(self, work_dir: Path, ext: str):
        from app.sandbox.output_files import find_file_in_output
        return find_file_in_output(work_dir, ext)

    def _build_step_context(self, plan: CADPlan, failed_steps: list[str]) -> str:
        parts = []
        if plan:
            parts.append(f"零件: {plan.description}")
            if plan.dimensions:
                dims_str = ", ".join(f"{k}={v}mm" for k, v in plan.dimensions.items())
                parts.append(f"尺寸: {dims_str}")
            if plan.features:
                parts.append(f"特征: {', '.join(plan.features)}")
        if failed_steps:
            parts.append(f"注意: 以下步骤未成功，请在当前步骤中尽量补全: {'; '.join(failed_steps)}")
        return "\n".join(parts)

    def _copy_output_files(
        self, work_dir: Path, request_id: str, output_formats: list[str]
    ) -> dict[str, str]:
        from app.sandbox.output_files import copy_output_files
        return copy_output_files(work_dir, request_id, output_formats)

    def _extract_params(self, code: str) -> dict[str, ParamConfig]:
        from app.sandbox.output_files import extract_params
        return extract_params(code)


async def _call_step(on_step, step: StepUpdate):
    import asyncio

    result = on_step(ensure_timeline_fields(step))
    if asyncio.iscoroutine(result):
        await result
