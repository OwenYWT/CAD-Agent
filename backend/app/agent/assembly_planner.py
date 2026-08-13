import json
import logging
import time

from pydantic import BaseModel

from app.config import settings, make_llm_client
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
7. \u4f4d\u7f6e\u5750\u6807\u4ee5 mm \u4e3a\u5355\u4f4d\uff0c\u88c5\u914d\u4f53\u4e2d\u5fc3\u5728\u539f\u70b9\u3002

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

    async def plan_assembly(self, plan: CADPlan) -> AssemblyPlan:
        user_content = (
            f"\u88c5\u914d\u4f53\u63cf\u8ff0: {plan.description}\n"
            f"\u5c3a\u5bf8: {plan.dimensions}\n"
            f"\u7279\u5f81: {plan.features}\n"
        )

        t0 = time.time()
        logger.info("AssemblyPlanner LLM call start")
        response = await self.client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=2048,
            temperature=0.1,
            messages=[
                {"role": "system", "content": ASSEMBLY_DECOMPOSE_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        logger.info(f"AssemblyPlanner LLM call done in {time.time() - t0:.1f}s")

        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            parsed = json.loads(text)
            parts = [AssemblyPart(**p) for p in parsed.get("parts", [])]
            if not parts:
                raise ValueError("No parts in plan")
            return AssemblyPlan(
                parts=parts[:8],
                assembly_description=parsed.get("assembly_description", plan.description),
            )
        except Exception as e:
            logger.warning(f"AssemblyPlanner parse failed: {e}, falling back to single part")
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
