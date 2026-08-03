import json
import logging
import re
import time


from app.agent.design_brief import ensure_design_brief
from app.agent.prompts import PLANNER_SYSTEM_PROMPT
from app.config import settings, make_llm_client
from app.llm import find_provider_exception
from app.models.schemas import CADPlan, ModificationPlan

logger = logging.getLogger(__name__)

MODIFICATION_SYSTEM_PROMPT = """你是一个 CAD 修改需求分析专家。分析用户对已有零件的修改请求。

当前代码:
```python
{current_code}
```

将修改请求解析为 JSON (不要输出其他任何文字):
{{
    "description": "修改描述",
    "modification_type": "dimension_change|add_feature|remove_feature|redesign",
    "target_params": {{"param_name": new_value}},
    "new_features": ["feature description"]
}}"""


class Planner:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not settings.has_llm_credentials:
                raise RuntimeError(settings.llm_credentials_error)
            self._client = make_llm_client()
        return self._client

    _ASSEMBLY_KEYWORDS = re.compile(
        r"装配|组装|底座.*柱子|多个零件|assembly|assemble|多个.*组合"
        r"|减速器|减速箱|变速箱|齿轮箱|gearbox|reducer"
        r"|逆止器|单向离合|backstop|overrunning"
    )


    @staticmethod
    def _parse_plan_payload(data: dict) -> CADPlan:
        normalized = dict(data)
        raw_brief = data.get("design_brief")
        if isinstance(raw_brief, dict):
            brief = dict(raw_brief)
            dimensions = []
            invalid_values = 0
            for raw_dimension in raw_brief.get("critical_dimensions") or []:
                if not isinstance(raw_dimension, dict):
                    dimensions.append(raw_dimension)
                    continue
                dimension = dict(raw_dimension)
                value = dimension.get("value")
                if value is not None:
                    try:
                        float(value)
                    except (TypeError, ValueError):
                        # The optional brief sometimes describes a composite
                        # envelope such as "120×90×60" in one value. The primary
                        # CADPlan dimensions remain the numeric source of truth;
                        # preserve the label/reason but mark this scalar unknown.
                        dimension["value"] = None
                        invalid_values += 1
                dimensions.append(dimension)
            brief["critical_dimensions"] = dimensions
            normalized["design_brief"] = brief
            if invalid_values:
                logger.warning(
                    "Planner ignored %d non-scalar optional brief dimension value(s)",
                    invalid_values,
                )

        plan = CADPlan(**normalized)
        ensure_design_brief(plan)
        return plan

    async def plan_new(self, messages: list[dict]) -> CADPlan:
        for attempt in range(2):
            try:
                t0 = time.time()
                logger.info(f"Planner LLM call start (model={settings.llm_model}, attempt={attempt+1})")
                response = await self.client.chat.completions.create(
                    model=settings.llm_model,
                    max_tokens=settings.planner_max_tokens,
                    temperature=0.1,
                    messages=[{"role": "system", "content": PLANNER_SYSTEM_PROMPT}] + messages,
                    response_format={"type": "json_object"},
                )
                elapsed = time.time() - t0
                choice = response.choices[0]
                finish_reason = getattr(choice, "finish_reason", None)
                logger.info(f"Planner LLM call done in {elapsed:.1f}s (stop={finish_reason})")
                if finish_reason == "length":
                    raise ValueError(
                        f"planner output was truncated at {settings.planner_max_tokens} completion tokens"
                    )
                content = choice.message.content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("planner model returned empty content")
                text = content.strip()
                # Strip markdown code fences if present
                if text.startswith("```"):
                    text = text.split("\n", 1)[1]
                    if text.endswith("```"):
                        text = text[:-3]
                    text = text.strip()
                parsed = json.loads(text)
                plan = self._parse_plan_payload(parsed)

                # Detect assembly intent from user message
                user_text = messages[-1]["content"] if messages else ""
                if self._ASSEMBLY_KEYWORDS.search(user_text):
                    plan.part_type = "assembly"

                return plan
            except RuntimeError:
                # Missing credentials / unrecoverable config — propagate, don't mask.
                raise
            except Exception as e:
                if find_provider_exception(e) is not None:
                    logger.warning(
                        "Planner provider request is non-retryable: %s",
                        type(e).__name__,
                    )
                    raise
                # Recoverable: malformed JSON, schema validation, transient API errors.
                # Log unexpected types with traceback so real bugs aren't hidden.
                if isinstance(e, (json.JSONDecodeError, ValueError, TypeError)):
                    logger.warning(f"Planner attempt {attempt + 1} parse/validation failed: {e}")
                else:
                    logger.warning(f"Planner attempt {attempt + 1} failed: {e}", exc_info=True)
                if attempt == 0:
                    continue
                raise ValueError("planner model did not return a valid CAD plan") from e

    async def plan_modification(self, messages: list[dict], current_code: str) -> ModificationPlan:
        system = MODIFICATION_SYSTEM_PROMPT.format(current_code=current_code)
        for attempt in range(2):
            try:
                t0 = time.time()
                logger.info(f"Modification planner LLM call start (attempt={attempt+1})")
                response = await self.client.chat.completions.create(
                    model=settings.llm_model,
                    max_tokens=settings.planner_max_tokens,
                    temperature=0.1,
                    messages=[{"role": "system", "content": system}] + messages,
                    response_format={"type": "json_object"},
                )
                elapsed = time.time() - t0
                choice = response.choices[0]
                finish_reason = getattr(choice, "finish_reason", None)
                logger.info(f"Modification planner LLM call done in {elapsed:.1f}s (stop={finish_reason})")
                if finish_reason == "length":
                    raise ValueError(
                        f"modification planner output was truncated at {settings.planner_max_tokens} completion tokens"
                    )
                content = choice.message.content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("modification planner model returned empty content")
                text = content.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1]
                    if text.endswith("```"):
                        text = text[:-3]
                    text = text.strip()
                parsed = json.loads(text)
                return ModificationPlan(**parsed)
            except RuntimeError:
                raise
            except Exception as e:
                if find_provider_exception(e) is not None:
                    logger.warning(
                        "Modification planner provider request is non-retryable: %s",
                        type(e).__name__,
                    )
                    raise
                if isinstance(e, (json.JSONDecodeError, ValueError, TypeError)):
                    logger.warning(f"Modification planner attempt {attempt + 1} parse/validation failed: {e}")
                else:
                    logger.warning(f"Modification planner attempt {attempt + 1} failed: {e}", exc_info=True)
                if attempt == 0:
                    continue
                raise ValueError("planner model did not return a valid modification plan") from e
