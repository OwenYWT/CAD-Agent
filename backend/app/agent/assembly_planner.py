import json
import logging
import time

from pydantic import BaseModel

from app.config import settings, make_llm_client
from app.llm import find_provider_exception
from app.models.schemas import CADPlan

logger = logging.getLogger(__name__)


class AssemblyPart(BaseModel):
    name: str
    description: str
    dimensions: dict[str, float] = {}
    position: list[float] = [0, 0, 0]
    color: str = "lightgray"


class AssemblyPlan(BaseModel):
    parts: list[AssemblyPart]
    assembly_description: str


ASSEMBLY_DECOMPOSE_PROMPT = """\u4f60\u662f CAD \u88c5\u914d\u4f53\u8bbe\u8ba1\u4e13\u5bb6\u3002\u8bf7\u628a\u88c5\u914d\u4f53\u63cf\u8ff0\u62c6\u89e3\u4e3a\u53ef\u5355\u72ec\u7f16\u8f91\u7684\u72ec\u7acb\u96f6\u4ef6\u6e05\u5355\u3002

\u89c4\u5219:
1. \u6bcf\u4e2a\u96f6\u4ef6\u5fc5\u987b\u662f\u53ef\u4ee5\u72ec\u7acb\u5efa\u6a21\u3001\u72ec\u7acb\u4fee\u6539\u7684\u5b9e\u4f53\u3002
2. \u591a\u4e2a\u5806\u53e0\u3001\u5e76\u6392\u3001\u9635\u5217\u3001\u4e0a\u4e0b\u7ec4\u5408\u7684\u76f8\u540c\u51e0\u4f55\u4f53\uff0c\u4e5f\u8981\u62c6\u6210\u591a\u4e2a\u72ec\u7acb\u96f6\u4ef6\u3002
3. \u4f7f\u7528\u7b80\u5316\u51e0\u4f55\uff0c\u4e0d\u8981\u5c1d\u8bd5\u7cbe\u786e\u9f7f\u5f62\u3001\u87ba\u7eb9\u6216\u590d\u6742\u66f2\u9762\u3002
4. \u6bcf\u4e2a\u96f6\u4ef6\u7ed9\u51fa name\u3001description\uff08\u542b\u5173\u952e\u5c3a\u5bf8\uff09\u3001dimensions\u3001position [x,y,z] \u548c color\u3002
5. \u989c\u8272\u4ece\u4ee5\u4e0b\u503c\u9009\u62e9: lightgray, steelblue, orange, green, red, gold, silver\u3002
6. \u6700\u591a 8 \u4e2a\u96f6\u4ef6\uff0c\u4f18\u5148\u62c6\u5206\u4e3b\u8981\u7ed3\u6784\u4ef6\u3002
7. \u4f4d\u7f6e\u5750\u6807\u4ee5 mm \u4e3a\u5355\u4f4d\u3002position [x,y,z] \u4e25\u683c\u8868\u793a\u96f6\u4ef6\u8f74\u5411\u5305\u56f4\u76d2\u5e95\u9762\u4e2d\u5fc3\u5728\u88c5\u914d\u5750\u6807\u7cfb\u4e2d\u7684\u76ee\u6807\u70b9\uff1ax/y \u662f\u5e95\u9762\u4e2d\u5fc3\uff0cz \u662f\u5e95\u9762\u9ad8\u5ea6\u3002\u4e0d\u5f97\u628a position \u89e3\u91ca\u4e3a\u5305\u56f4\u76d2\u4e2d\u5fc3\u3002
8. \u7528\u6237\u660e\u786e\u7ed9\u51fa position \u65f6\u5fc5\u987b\u539f\u6837\u4fdd\u7559\uff1b\u7528\u6237\u672a\u7ed9\u51fa\u65f6\uff0c\u518d\u6309\u5e95\u9762\u4e2d\u5fc3\u8bed\u4e49\u89c4\u5212\u65e0\u91cd\u53e0\u7684\u9ed8\u8ba4\u5e03\u5c40\u3002

\u8f93\u51fa JSON\uff08\u4e0d\u8981\u8f93\u51fa\u5176\u4ed6\u6587\u5b57\uff09:
{
    "assembly_description": "\u56db\u4e2a\u72ec\u7acb\u6b63\u65b9\u4f53\u7531\u4e0a\u5230\u4e0b\u5806\u53e0",
    "parts": [
        {
            "name": "cube_top",
            "description": "\u9876\u90e8\u72ec\u7acb\u6b63\u65b9\u4f53\uff0c\u8fb9\u957f20mm",
            "dimensions": {"side": 20},
            "position": [0, 0, 120],
            "color": "steelblue"
        }
    ]
}"""


class AssemblyPlanner:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not settings.has_llm_credentials:
                raise RuntimeError(settings.llm_credentials_error)
            self._client = make_llm_client()
        return self._client

    async def plan_assembly(
        self,
        plan: CADPlan,
        *,
        allow_fallback: bool = True,
    ) -> AssemblyPlan:
        user_content = (
            f"\u88c5\u914d\u4f53\u63cf\u8ff0: {plan.description}\n"
            f"\u5c3a\u5bf8: {plan.dimensions}\n"
            f"\u7279\u5f81: {plan.features}\n"
        )

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                messages = [
                    {"role": "system", "content": ASSEMBLY_DECOMPOSE_PROMPT},
                    {"role": "user", "content": user_content},
                ]
                if last_error is not None:
                    messages.append({
                        "role": "user",
                        "content": (
                            f"上次输出未通过校验：{str(last_error)[:1000]}。"
                            "请重新输出完整、精简的 JSON 装配方案，不要输出解释文字。"
                            "保留原需求中的明确尺寸和结构，不要为了缩短输出省略必要零件。"
                        ),
                    })
                t0 = time.time()
                logger.info(
                    "AssemblyPlanner LLM call start (attempt=%d)",
                    attempt + 1,
                )
                response = await self.client.chat.completions.create(
                    model=settings.llm_model,
                    stream=True,
                    temperature=0.1,
                    messages=messages,
                    response_format={"type": "json_object"},
                )
                choice = response.choices[0]
                finish_reason = getattr(choice, "finish_reason", None)
                logger.info(
                    "AssemblyPlanner LLM call done in %.1fs (stop=%s)",
                    time.time() - t0,
                    finish_reason,
                )
                if finish_reason == "length":
                    raise ValueError(
                        "assembly planner output was truncated by the model service"
                    )
                content = choice.message.content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("assembly planner model returned empty content")
                text = content.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1]
                    if text.endswith("```"):
                        text = text[:-3]
                    text = text.strip()

                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    raise ValueError(
                        "assembly planner response must be a JSON object"
                    )
                parts = [
                    AssemblyPart(**item)
                    for item in parsed.get("parts", [])
                ]
                if not parts:
                    raise ValueError("assembly planner returned no parts")
                return AssemblyPlan(
                    parts=parts[:8],
                    assembly_description=parsed.get(
                        "assembly_description",
                        plan.description,
                    ),
                )
            except RuntimeError:
                raise
            except Exception as exc:
                if find_provider_exception(exc) is not None:
                    raise
                last_error = exc
                logger.warning(
                    "AssemblyPlanner attempt %d parse/validation failed: %s",
                    attempt + 1,
                    exc,
                )

        if not allow_fallback:
            raise ValueError(
                "assembly planner model did not return a valid assembly plan: "
                f"{str(last_error)[:1000]}"
            ) from last_error
        logger.warning(
            "AssemblyPlanner exhausted valid responses; falling back to single part"
        )
        return AssemblyPlan(
            parts=[
                AssemblyPart(
                    name="main",
                    description=plan.description,
                    dimensions=plan.dimensions,
                )
            ],
            assembly_description=plan.description,
        )
