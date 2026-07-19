import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


from app.config import settings, make_llm_client
from app.agent.run_steps import ensure_timeline_fields, make_step
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


DECOMPOSE_PROMPT = """浣犳槸 CAD 鏋勫缓瑙勫垝涓撳銆傚皢闆朵欢鎻忚堪鍒嗚В涓烘湁搴忔瀯寤烘楠ゃ€?

## 瑙勫垯
1. 绗竴姝ユ€绘槸 phase="base"锛屽垱寤哄熀纭€褰綋
2. 鐒跺悗 phase="primary": 鎶藉３銆佷富瑕侀€氬瓟銆佸嚫鍙?
3. 鐒跺悗 phase="secondary": 瀹夎瀛斻€佹暎鐑瓔銆佸噺閲嶆Ы銆佸畨瑁呰€崇瓑锛堟瘡绉嶇壒寰佸崟鐙竴姝ワ級
4. 鏈€鍚?phase="refinement": 鍦嗚銆佸€掕
5. 绠€鍗曢浂浠?(鈮? 鐗瑰緛) 杩斿洖 1-2 姝ワ紝涓嶈杩囧害鍒嗚В
6. 鏈€澶?10 姝ワ紝澶嶆潅闆朵欢搴斿厖鍒嗗垎姝ワ紝姣忔鍙仛涓€绉嶇壒寰?
7. 澶嶆潅宸ヤ笟闆朵欢 (鍑忛€熷櫒/榻胯疆绠?閫嗘鍣ㄧ瓑): 鐢ㄧ畝鍖栧嚑浣曪紝涓嶈灏濊瘯绮剧‘榻垮舰/铻虹汗
8. 瑁呴厤浣? 姣忎釜闆朵欢鐢ㄥ嚱鏁板畾涔夛紝鏈€鍚庣敤 cq.Assembly() 缁勮

## 鍏抽敭瑕佹眰
- description 涓繀椤诲寘鍚?*绮剧‘鐨勬暟鍊煎弬鏁?*锛堝昂瀵搞€佷綅缃€佹暟閲忋€侀棿璺濈瓑锛?
- 涓嶈鍐欐ā绯婃弿杩板"娣诲姞瀹夎瀛?锛岃鍐?鍦ㄥ簳闈㈠洓瑙掓坊鍔?涓狹3閫氬瓟(桅3.4mm)锛屾寜120x60mm鐭╁舰闃靛垪鍒嗗竷"
- 姣忔鐨?description 蹇呴』鑷寘鍚紝LLM 浠呭嚟 description 灏辫兘鍐欏嚭姝ｇ‘浠ｇ爜
- 姣忔鍙仛鐒︿竴绉嶇壒寰侊紝闄嶄綆鍗曟澶嶆潅搴?

## 杈撳嚭 JSON (涓嶈杈撳嚭鍏朵粬鏂囧瓧):
{
    "complexity": "complex",
    "steps": [
        {"phase": "base", "description": "鍒涘缓 160x100x35mm 闀挎柟浣擄紝鍘熺偣鍦ㄥ簳闈腑蹇?},
        {"phase": "primary", "description": "浠庨《闈?>Z)鎶藉３锛屽鍘?mm锛屽簳閮ㄤ繚鐣?},
        {"phase": "secondary", "description": "鍐呴儴搴曢潰鍥涜娣诲姞4涓狹3铻轰笣鏌憋細澶栧緞6mm锛屽唴瀛斘?.5mm锛岄珮搴?5mm锛屼綅缃寜120x70mm鐭╁舰闃靛垪"},
        {"phase": "secondary", "description": "澶栦晶闀胯竟(>Y鍜?Y闈?鍚勬坊鍔?鏉℃暎鐑瓔锛氬2mm锛岄珮3mm锛岄棿璺?0mm锛屼粠澹充綋澶栧闈㈠嚫鍑猴紝鍏?鏉★紝鐢╱nion鍚堝苟鍒颁富浣?},
        {"phase": "secondary", "description": "搴曢儴澶栬〃闈㈡坊鍔犺渹绐濈姸鍑忛噸妲斤細鍏竟褰㈤樀鍒楋紝鍏竟褰㈠鎺ュ渾鐩村緞10mm锛岄棿璺?2mm锛屾繁搴?.5mm锛岀敤cut浠庡簳闈㈡寲闄?},
        {"phase": "secondary", "description": "宸﹀彸涓や晶(<X鍜?X闈?鍚勬坊鍔?涓畨瑁呰€筹細20x15x3mm鍑稿彴锛屼粠渚у澶栬〃闈㈠欢浼革紝涓績鏈壩?.5mm閫氬瓟锛岀敤union鍚堝苟鍒颁富浣?},
        {"phase": "refinement", "description": "鎵€鏈夊杈圭紭鍊掕1mm"}
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
            f"闆朵欢鎻忚堪: {plan.description}\n"
            f"绫诲瀷: {plan.part_type}\n"
            f"灏哄: {plan.dimensions}\n"
            f"鐗瑰緛: {plan.features}\n"
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

        for i, step in enumerate(build_plan.steps):
            if on_step:
                await _call_step(
                    on_step,
                    StepUpdate(
                        step="multi_step",
                        message=f"姝ラ {i + 1}/{total}: {step.description}",
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
                            message=f"姝ラ {i + 1} 淇涓?(灏濊瘯 {attempt}/{self.MAX_STEP_RETRIES})",
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
                finally:
                    shutil.rmtree(result.work_dir, ignore_errors=True)

            if not step_succeeded:
                step.error = last_error_msg
                failed_steps.append(step.description)
                logger.warning(f"Step {i + 1} failed after {self.MAX_STEP_RETRIES} retries: {step.error}")

        # Final execution with show_object
        final_code = accumulated_code + "\nshow_object(result)"

        if on_step:
            await _call_step(on_step, StepUpdate(step="executing", message="姝ｅ湪鎵ц鏈€缁堜唬鐮?.."))

        final_result = await self.executor.execute(final_code)

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
                                message=f"鍑犱綍鏍￠獙瀹屾垚 (浣撶Н: {vol_str}mm鲁)",
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
                                round_msg = f"姝ｅ湪杩涜瑙嗚鏍￠獙 ({vision_round + 1}/2)..." if vision_round > 0 else "姝ｅ湪杩涜瑙嗚鏍￠獙..."
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
                                        step="executing", message="瑙嗚鏍￠獙閫氳繃"
                                    ))
                                break

                            logger.info(f"Vision round {vision_round + 1} issues: {vision_result.issues}")
                            if on_step:
                                issues_str = "; ".join(vision_result.issues[:2])
                                await _call_step(on_step, StepUpdate(
                                    step="fixing_error",
                                    message=f"瑙嗚鏍￠獙涓嶉€氳繃 ({vision_round + 1}/2)锛屾鍦ㄤ慨澶? {issues_str}",
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
                                        step="executing", message=f"瑙嗚淇绗?{vision_round + 1} 杞湭鎴愬姛"
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
            parts.append(f"闆朵欢: {plan.description}")
            if plan.dimensions:
                dims_str = ", ".join(f"{k}={v}mm" for k, v in plan.dimensions.items())
                parts.append(f"灏哄: {dims_str}")
            if plan.features:
                parts.append(f"鐗瑰緛: {', '.join(plan.features)}")
        if failed_steps:
            parts.append(f"娉ㄦ剰: 浠ヤ笅姝ラ鏈垚鍔燂紝璇峰湪褰撳墠姝ラ涓敖閲忚ˉ鍏? {'; '.join(failed_steps)}")
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

