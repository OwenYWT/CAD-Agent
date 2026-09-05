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

MODIFICATION_SYSTEM_PROMPT = """\u4f60\u662f\u4e00\u4e2a CAD \u4fee\u6539\u9700\u6c42\u5206\u6790\u4e13\u5bb6\u3002\u8bf7\u5206\u6790\u7528\u6237\u5bf9\u5df2\u6709\u96f6\u4ef6\u7684\u4fee\u6539\u8bf7\u6c42\u3002

\u5f53\u524d\u53ef\u7f16\u8f91\u6a21\u578b\u4e0a\u4e0b\u6587\uff08\u53ef\u80fd\u662f MCAD \u6e90\u7801\uff0c\u4e5f\u53ef\u80fd\u662f\u7ed3\u6784\u5316 FreeCAD \u72b6\u6001\uff09:
```
{current_code}
```

\u5c06\u4fee\u6539\u8bf7\u6c42\u89e3\u6790\u4e3a JSON\uff08\u4e0d\u8981\u8f93\u51fa\u5176\u4ed6\u4efb\u4f55\u6587\u5b57\uff09:
{{
    "description": "\u4fee\u6539\u63cf\u8ff0",
    "modification_type": "dimension_change|add_feature|remove_feature|redesign",
    "target_params": {{"param_name": new_value}},
    "new_features": ["feature description"]
}}"""


class Planner:
    def __init__(self):
        self._client = None

    @staticmethod
    def _parse_modification_payload(data: dict) -> ModificationPlan:
        normalized = dict(data)
        raw_params = data.get("target_params") or {}
        if not isinstance(raw_params, dict):
            return ModificationPlan(**normalized)
        params: dict[str, float | object] = {}
        unit_scales = {"": 1.0, "mm": 1.0, "cm": 10.0, "in": 25.4, "inch": 25.4}
        for key, value in raw_params.items():
            if isinstance(value, str):
                match = re.fullmatch(
                    r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
                    r"(mm|cm|in|inch)?\s*",
                    value,
                    re.IGNORECASE,
                )
                if match is not None:
                    unit = (match.group(2) or "").lower()
                    value = float(match.group(1)) * unit_scales[unit]
            params[str(key)] = value
        normalized["target_params"] = params
        return ModificationPlan(**normalized)

    @property
    def client(self):
        if self._client is None:
            if not settings.has_llm_credentials:
                raise RuntimeError(settings.llm_credentials_error)
            self._client = make_llm_client()
        return self._client

    _ASSEMBLY_KEYWORDS = re.compile(
        r"\u88c5\u914d|\u88c5\u914d\u4f53|\u7ec4\u88c5|\u7ec4\u4ef6|\u7ec4\u5408\u4f53|\u72ec\u7acb\u96f6\u4ef6|\u591a\u4e2a\u96f6\u4ef6|\u591a\u4e2a\u90e8\u4ef6|\u5206\u522b\u5efa\u6a21"
        r"|\u591a\u4e2a.*\u7ec4\u5408|\u591a\u4e2a.*\u645e|[2-9]\u4e2a.*(\u6b63\u65b9\u4f53|\u65b9\u5757|\u96f6\u4ef6|\u90e8\u4ef6)|\u7531\u4e0a\u5230\u4e0b.*\u645e|\u4ece\u4e0a\u5230\u4e0b.*\u645e"
        r"|assembly|assemble|gearbox|reducer|backstop|overrunning",
        re.IGNORECASE,
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

                # Detect assembly intent from user message.
                user_text = messages[-1]["content"] if messages else ""
                if self._ASSEMBLY_KEYWORDS.search(user_text):
                    plan.part_type = "assembly"

                return plan
            except RuntimeError:
                # Missing credentials / unrecoverable config; propagate, don't mask.
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
                logger.info(
                    "Modification planner LLM call done in %.1fs (stop=%s)",
                    elapsed,
                    finish_reason,
                )
                if finish_reason == "length":
                    raise ValueError(
                        "modification planner output was truncated at "
                        f"{settings.planner_max_tokens} completion tokens"
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
                return self._parse_modification_payload(parsed)
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
