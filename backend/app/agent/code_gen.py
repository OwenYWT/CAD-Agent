import json
import logging
import math
import textwrap
import time


from app.agent.failure_taxonomy import classify, fix_hint_for, render_prompt_table
from app.agent.prompts import CODEGEN_SYSTEM_PROMPT, MODIFICATION_PROMPT, ERROR_FIX_PROMPT, EZDXF_CODEGEN_PROMPT, ASSEMBLY_CODEGEN_PROMPT
from app.config import settings, make_llm_client
from app.models.schemas import CADPlan, ModificationPlan

logger = logging.getLogger(__name__)


class CodeGenerator:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not settings.has_llm_credentials:
                raise RuntimeError(settings.llm_credentials_error)
            self._client = make_llm_client()
        return self._client


    @staticmethod
    def _format_design_brief_context(plan: CADPlan) -> str:
        brief = plan.design_brief
        if not brief:
            return ""

        dimension_lines = [
            f"- {item.name}={item.value} {item.unit}: {item.reason}"
            if item.value is not None
            else f"- {item.name}: {item.reason}"
            for item in brief.critical_dimensions
        ]
        sections = [
            "Engineering Brief:",
            f"Intent: {brief.intent_summary}",
            f"Artifact type: {brief.artifact_type}",
            f"Manufacturing posture: {brief.manufacturing_posture}",
        ]
        if brief.assumptions:
            sections.append("Assumptions:\n" + "\n".join(f"- {item}" for item in brief.assumptions))
        if dimension_lines:
            sections.append("Critical dimensions:\n" + "\n".join(dimension_lines))
        if brief.functional_requirements:
            sections.append("Functional requirements:\n" + "\n".join(f"- {item}" for item in brief.functional_requirements))
        if brief.printability_targets:
            sections.append("Printability targets:\n" + "\n".join(f"- {item}" for item in brief.printability_targets))
        if brief.acceptance_criteria:
            sections.append("Acceptance criteria:\n" + "\n".join(f"- {item}" for item in brief.acceptance_criteria))
        return "\n\n".join(sections)

    @staticmethod
    def _format_repair_context(value) -> str:
        if value is None:
            return ""
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            payload = model_dump(mode="json")
        elif isinstance(value, dict):
            payload = value
        else:
            payload = {"value": str(value)}
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    async def generate(
        self, plan: CADPlan, examples: list[dict], conversation: list[dict],
        extra_context: str = "",
    ) -> str:
        system = CODEGEN_SYSTEM_PROMPT.format(examples=self._format_examples(examples))
        if extra_context:
            system += extra_context

        user_content = (
            f"需求描述: {plan.description}\n"
            f"零件类型: {plan.part_type}\n"
            f"建模策略: {plan.modeling_hint or 'extrude_cut'}\n"
            f"尺寸: {plan.dimensions}\n"
            f"特征: {plan.features}\n"
            f"约束: {plan.constraints}\n"
        )
        if plan.ambiguities:
            user_content += f"注意: {plan.ambiguities}\n"

        brief_context = self._format_design_brief_context(plan)
        if brief_context:
            user_content += f"\n{brief_context}\n"

        messages = conversation + [{"role": "user", "content": user_content}]
        # Ensure messages alternate correctly: just use the last user message
        messages = [{"role": "user", "content": user_content}]

        t0 = time.time()
        logger.info(f"CodeGen.generate LLM call start (model={settings.llm_model})")
        response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=4096,
            temperature=0.2,
            messages=[{"role": "system", "content": system}] + messages,
        )
        logger.info(f"CodeGen.generate LLM call done in {time.time()-t0:.1f}s")

        text = response.choices[0].message.content
        return self._extract_code(text)

    async def generate_2d(
        self, plan: CADPlan, examples: list[dict], conversation: list[dict]
    ) -> str:
        system = EZDXF_CODEGEN_PROMPT.format(examples=self._format_examples(examples))

        user_content = (
            f"需求描述: {plan.description}\n"
            f"零件类型: 2D 轮廓\n"
            f"尺寸: {plan.dimensions}\n"
            f"特征: {plan.features}\n"
        )

        brief_context = self._format_design_brief_context(plan)
        if brief_context:
            user_content += f"\n{brief_context}\n"

        messages = [{"role": "user", "content": user_content}]

        t0 = time.time()
        logger.info("CodeGen.generate_2d LLM call start")
        response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=4096,
            temperature=0.2,
            messages=[{"role": "system", "content": system}] + messages,
        )
        logger.info(f"CodeGen.generate_2d LLM call done in {time.time()-t0:.1f}s")

        text = response.choices[0].message.content
        return self._extract_code(text)

    async def generate_assembly(
        self, plan: CADPlan, examples: list[dict], conversation: list[dict]
    ) -> str:
        system = ASSEMBLY_CODEGEN_PROMPT.format(examples=self._format_examples(examples))

        user_content = (
            f"需求描述: {plan.description}\n"
            f"零件类型: 装配体\n"
            f"尺寸: {plan.dimensions}\n"
            f"特征: {plan.features}\n"
        )

        brief_context = self._format_design_brief_context(plan)
        if brief_context:
            user_content += f"\n{brief_context}\n"

        messages = [{"role": "user", "content": user_content}]

        t0 = time.time()
        logger.info("CodeGen.generate_assembly LLM call start")
        response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=8192,
            temperature=0.2,
            messages=[{"role": "system", "content": system}] + messages,
        )
        logger.info(f"CodeGen.generate_assembly LLM call done in {time.time()-t0:.1f}s (stop={response.choices[0].finish_reason})")

        text = response.choices[0].message.content
        code = self._extract_code(text)

        if response.choices[0].finish_reason == "length":
            logger.warning("Assembly code was truncated by max_tokens, attempting to close")
            code = self._close_truncated_code(code)

        return code

    async def modify(
        self,
        plan: ModificationPlan,
        existing_code: str,
        examples: list[dict],
        conversation: list[dict],
    ) -> str:
        system = MODIFICATION_PROMPT.format(
            existing_code=existing_code,
            modification_description=plan.description,
        )

        messages = [{"role": "user", "content": f"修改要求: {plan.description}"}]

        t0 = time.time()
        logger.info("CodeGen.modify LLM call start")
        response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=4096,
            temperature=0.2,
            messages=[{"role": "system", "content": system}] + messages,
        )
        logger.info(f"CodeGen.modify LLM call done in {time.time()-t0:.1f}s")

        text = response.choices[0].message.content
        return self._extract_code(text)

    async def fix_error(
        self, code: str, error: dict, plan: CADPlan | None = None
    ) -> str:
        system = ERROR_FIX_PROMPT.format(
            error_table=render_prompt_table(),
            code=code,
            error_type=error.get("type", "Unknown"),
            error_message=error.get("message", ""),
            traceback=error.get("traceback", ""),
        )
        repair_context = error.get("context")
        if repair_context is not None:
            system += (
                "\n\n## Repair Context\n"
                f"```json\n{self._format_repair_context(repair_context)}\n```"
            )
        elif plan is not None:
            system += (
                "\n\n## Repair Plan\n"
                f"```json\n{self._format_repair_context(plan)}\n```"
            )

        # Classify the failure → targeted hint + minimal-change directive (A1 + A3).
        fc = classify(
            error.get("type"), error.get("message", ""),
            error.get("traceback", ""), error.get("gate"),
        )
        hint = f"\n\n具体修复建议: {fix_hint_for(fc)}"

        messages = [{"role": "user", "content": f"请修复上述代码错误。{hint}"}]

        t0 = time.time()
        logger.info(f"CodeGen.fix_error LLM call start (class={fc.key})")
        response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=8192,
            temperature=0.1,
            messages=[{"role": "system", "content": system}] + messages,
        )
        logger.info(f"CodeGen.fix_error LLM call done in {time.time()-t0:.1f}s")

        text = response.choices[0].message.content
        return self._extract_code(text)

    async def generate_step(
        self,
        step_description: str,
        accumulated_code: str,
        step_index: int,
        total_steps: int,
        plan_context: str = "",
    ) -> str:
        context_section = f"\n## 零件上下文\n{plan_context}\n" if plan_context else ""

        system = (
            "你正在分步构建 CAD 零件。当前已有以下代码:\n"
            f"```python\n{accumulated_code}\n```\n"
            f"{context_section}\n"
            f"请为以下步骤生成代码片段 (不要重复 import 和已有代码，只写新增部分):\n"
            f"{step_description}\n\n"
            "## 关键规则\n"
            "1. 代码片段末尾必须将 result 变量设为最终合并后的单一实体\n"
            "2. 新建的子特征（筋条、凸台、安装耳、减重槽等）必须用 result = result.union(feature) 或 result = result.cut(feature) 合并到主体\n"
            "3. 禁止创建独立的、未合并的实体。禁止调用 show_object()\n"
            "4. 如果需要在循环中创建阵列特征，每次循环都要 union/cut 到 result 上\n"
            "5. fillet/chamfer 半径不得超过最短边的 40%\n"
            "6. 所有尺寸单位为 mm，坐标系原点在零件底面中心，Z 轴向上\n\n"
            "只输出代码片段，不输出解释文字。"
        )

        messages = [
            {"role": "user", "content": f"步骤 {step_index + 1}/{total_steps}: {step_description}"}
        ]

        t0 = time.time()
        logger.info(f"CodeGen.generate_step LLM call start (step {step_index+1}/{total_steps})")
        response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=4096,
            temperature=0.2,
            messages=[{"role": "system", "content": system}] + messages,
        )
        logger.info(f"CodeGen.generate_step LLM call done in {time.time()-t0:.1f}s")

        text = response.choices[0].message.content
        return self._extract_code(text)

    async def generate_single_part(
        self,
        part_name: str,
        part_description: str,
        part_dimensions: dict,
        examples: list[dict],
    ) -> str:
        """Generate code for a single part as a function make_{name}() -> cq.Workplane."""
        system = (
            "你是 CadQuery 编程专家。生成一个独立零件的完整代码。\n\n"
            "规则:\n"
            "1. 定义函数 make_{name}() 返回 cq.Workplane 对象\n"
            "2. 关键尺寸在函数外定义为变量\n"
            "3. 用简化几何，不需要精确齿形/螺纹\n"
            "4. 代码末尾: result = make_{name}()\n"
            "5. 最后一行: show_object(result)\n"
            "6. 只输出 Python 代码\n"
        ).format(name=part_name)

        if examples:
            system += self._format_examples(examples)

        user_content = (
            f"零件名称: {part_name}\n"
            f"零件描述: {part_description}\n"
            f"尺寸: {part_dimensions}\n"
        )

        t0 = time.time()
        logger.info(f"CodeGen.generate_single_part start: {part_name}")
        response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=4096,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )
        logger.info(f"CodeGen.generate_single_part done: {part_name} in {time.time()-t0:.1f}s")

        return self._extract_code(response.choices[0].message.content)

    async def generate_assembly_combiner(
        self,
        part_codes: list[dict],
    ) -> str:
        """Compose persisted part sources without rewriting them through an LLM.

        Every source runs in its own lexical scope.  This is required because
        independently generated parts routinely use the same parameter names
        (for example ``height``); concatenating them changes earlier functions'
        globals and therefore changes the already-validated component geometry.
        """
        if not part_codes:
            raise ValueError("assembly combine requires at least one part")

        component_blocks: list[str] = []
        add_lines: list[str] = []
        for index, part in enumerate(part_codes, 1):
            raw_source = str(part.get("code") or "").strip()
            if not raw_source:
                raise ValueError("assembly part source cannot be empty")
            clean_lines = [
                line
                for line in raw_source.splitlines()
                if not line.strip().startswith("show_object(")
                and not line.strip().startswith("from __future__ import ")
            ]
            clean_source = "\n".join(clean_lines).strip()
            if not clean_source:
                raise ValueError("assembly part source has no executable content")

            raw_position = part.get("position", [0, 0, 0])
            if not isinstance(raw_position, (list, tuple)) or len(raw_position) != 3:
                raise ValueError("assembly part position must contain three values")
            position = tuple(float(value) for value in raw_position)
            if any(not math.isfinite(value) for value in position):
                raise ValueError("assembly part position must be finite")

            component_name = f"_assembly_component_{index:02d}"
            component_blocks.append(
                f"def {component_name}():\n"
                f"{textwrap.indent(clean_source, '    ')}\n"
                "    return result"
            )
            child_name = str(part.get("name") or f"part_{index:02d}")
            color = str(part.get("color") or "lightgray")
            add_lines.append(
                "result.add("
                f"_assembly_anchor_to_origin({component_name}()), "
                f"name={child_name!r}, "
                f"loc=cq.Location({position!r}), color=cq.Color({color!r})"
                ")"
            )

        return (
            "import cadquery as cq\n\n"
            "def _assembly_anchor_to_origin(component):\n"
            "    bounds = component.val().BoundingBox()\n"
            "    # part_position targets the centre of the bottom bounding-box\n"
            "    # face.  Generated part sources may use different local origins,\n"
            "    # so normalise that documented anchor before assembly placement.\n"
            "    anchor = (\n"
            "        (bounds.xmin + bounds.xmax) / 2.0,\n"
            "        (bounds.ymin + bounds.ymax) / 2.0,\n"
            "        bounds.zmin,\n"
            "    )\n"
            "    return component.translate(tuple(-value for value in anchor))\n\n"
            + "\n\n".join(component_blocks)
            + "\n\nresult = cq.Assembly()\n"
            + "\n".join(add_lines)
            + "\n\nshow_object(result)\n"
        )

    async def fix_visual_issues(
        self, code: str, issues: list[str], suggestions: list[str],
        on_step=None,
    ) -> str:
        def completed_text(response, stage: str) -> str:
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                raise ValueError(
                    f"visual repair {stage} output was truncated at "
                    f"{settings.planner_max_tokens} completion tokens"
                )
            content = choice.message.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"visual repair {stage} returned empty content")
            return content.strip()

        issues_text = "\n".join(f"- {i}" for i in issues)
        suggestions_text = "\n".join(f"- {s}" for s in suggestions)

        # Step 1: Generate fix plan
        plan_system = (
            "你是 CadQuery 代码修复专家。以下代码生成的 3D 模型有视觉问题。\n\n"
            f"问题:\n{issues_text}\n\n"
            f"建议:\n{suggestions_text}\n\n"
            f"当前代码:\n```python\n{code}\n```\n\n"
            "请先分析每个问题的原因，然后列出具体的修复计划。\n\n"
            "## 关键约束\n"
            "- **禁止删除任何已有特征**。修复分离问题时，应修改合并方式（union/cut），而不是删除特征\n"
            "- 修复后的代码必须保留原代码中所有特征（孔、筋、柱、耳、槽等）\n"
            "- 如果某个特征无法合并，保留它并注明原因，不要删掉\n\n"
            "输出格式:\n"
            "## 问题分析\n"
            "1. 问题描述 → 原因\n\n"
            "## 修复计划\n"
            "1. 具体要改哪行、怎么改（只改合并方式，不删特征）\n\n"
            "只输出分析和计划，不输出代码。"
        )

        plan_response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=settings.planner_max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            temperature=0.1,
            messages=[
                {"role": "system", "content": plan_system},
                {"role": "user", "content": "请分析问题并制定修复计划。"},
            ],
        )

        fix_plan = completed_text(plan_response, "plan")
        logger.info(f"Visual fix plan:\n{fix_plan}")

        if on_step:
            from app.models.schemas import StepUpdate
            import asyncio
            step = StepUpdate(step="fixing_error", message=f"修复计划: {fix_plan[:200]}")
            result = on_step(step)
            if asyncio.iscoroutine(result):
                await result

        # Step 2: Apply fix based on plan
        fix_system = (
            f"根据以下修复计划，修改 CadQuery 代码:\n\n"
            f"修复计划:\n{fix_plan}\n\n"
            f"当前代码:\n```python\n{code}\n```\n\n"
            "规则:\n"
            "- **禁止删除原代码中的任何特征**，所有孔、筋、柱、耳、槽必须保留\n"
            "- 所有子特征必须 union/cut 到 result 上，不能有分离实体\n"
            "- 只允许一次 show_object(result)\n"
            "- fillet/chamfer 半径不超过最短边 40%\n"
            "- 如果 union 某个特征导致错误，用 try-except 包裹并保留主体\n\n"
            "只输出修复后的完整 Python 代码。"
        )

        fix_response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=settings.planner_max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            temperature=0.1,
            messages=[
                {"role": "system", "content": fix_system},
                {"role": "user", "content": "请按计划修复代码。"},
            ],
        )

        return self._extract_code(completed_text(fix_response, "code"))

    def _extract_code(self, text: str) -> str:
        text = text.strip()
        if "```python" in text:
            start = text.index("```python") + len("```python")
            # Find closing ``` or use rest of text
            end_idx = text.find("```", start)
            if end_idx == -1:
                return text[start:].strip()
            return text[start:end_idx].strip()
        if "```" in text:
            start = text.index("```") + 3
            # Skip language identifier if on the same line
            newline_idx = text.find("\n", start)
            if newline_idx == -1:
                return text[start:].strip()
            start = newline_idx + 1
            end_idx = text.find("```", start)
            if end_idx == -1:
                return text[start:].strip()
            return text[start:end_idx].strip()
        return text.strip()

    def _close_truncated_code(self, code: str) -> str:
        """Attempt to close truncated code by balancing brackets and adding show_object."""
        # Count unclosed brackets
        opens = {"(": ")", "[": "]", "{": "}"}
        closes = {v: k for k, v in opens.items()}
        stack: list[str] = []
        in_string = False
        string_char = ""

        for ch in code:
            if in_string:
                if ch == string_char:
                    in_string = False
                continue
            if ch in ('"', "'"):
                in_string = True
                string_char = ch
            elif ch in opens:
                stack.append(opens[ch])
            elif ch in closes:
                if stack and stack[-1] == ch:
                    stack.pop()

        # Close unclosed brackets
        closing = "".join(reversed(stack))
        if closing:
            code = code.rstrip() + closing + "\n"

        # Ensure show_object(result) exists
        if "show_object" not in code:
            code += "\nshow_object(result)\n"

        return code

    def _format_examples(self, examples: list[dict]) -> str:
        if not examples:
            return ""
        parts = ["\n## 可复用 CAD 案例\n"]
        for index, example in enumerate(examples, 1):
            parts.append(f"### 案例 {index}: {example['description']}")
            if example.get("part_type") or example.get("category"):
                parts.append(f"- Type: {example.get('part_type') or example.get('category', '')}")
            if example.get("features_used"):
                parts.append(f"- Proven features: {', '.join(example['features_used'])}")
            if example.get("modeling_hints"):
                parts.append(f"- Modeling hints: {', '.join(example['modeling_hints'])}")
            if example.get("manufacturing_notes"):
                parts.append("- Manufacturing notes: " + "; ".join(example["manufacturing_notes"]))
            if example.get("failure_modes"):
                parts.append("- Common failure modes: " + "; ".join(example["failure_modes"]))
            if example.get("print_profile"):
                parts.append(f"- Print profile: {example['print_profile']}")
            parts.append(f"```python\n{example['code']}\n```\n")
        parts.append("将这些案例作为建模模式和 DFM 约束参考。只复用方法，不要照搬无关尺寸。")
        return "\n".join(parts)
