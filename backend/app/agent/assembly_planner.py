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


ASSEMBLY_DECOMPOSE_PROMPT = """你是 CAD 装配体设计专家。将装配体描述拆解为独立零件清单。

规则:
1. 每个零件必须是可以独立建模的实体
2. 用简化几何，不要尝试精确齿形/螺纹/复杂曲面
3. 每个零件给出：名称、描述（含关键尺寸）、在装配体中的位置 [x,y,z]、颜色
4. 颜色从以下选择: lightgray, steelblue, orange, green, red, gold, silver
5. 最多 8 个零件，优先拆分主要结构件
6. 位置坐标以 mm 为单位，装配体中心在原点

输出 JSON (不要输出其他文字):
{
    "assembly_description": "减速器装配体",
    "parts": [
        {
            "name": "housing",
            "description": "减速器箱体，200x150x120mm，壁厚5mm，顶部开口",
            "dimensions": {"length": 200, "width": 150, "height": 120},
            "position": [0, 0, 0],
            "color": "lightgray"
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
            f"装配体描述: {plan.description}\n"
            f"尺寸: {plan.dimensions}\n"
            f"特征: {plan.features}\n"
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
