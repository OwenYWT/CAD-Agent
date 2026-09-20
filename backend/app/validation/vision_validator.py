import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path


from app.config import settings, make_llm_client

logger = logging.getLogger(__name__)

VISION_VALIDATION_PROMPT = """你是一个 CAD 质量检验专家。
你会看到一个 3D 模型的 4 张渲染图（前视/右视/顶视/等轴测），用户的原始描述，以及生成的代码摘要。

## 检查项

1. **形状类型**: 模型的整体形状是否匹配描述？
   - "盒子" 应该是六面体
   - "圆柱" 应该是圆柱形
   - "支架" 应该有明显的 L/U/T 形态
   - "壳体/外壳" 应该有内腔

2. **比例合理性**: 从视觉上看比例是否合理？
   - 100x60x40 的盒子不应该看起来像正方体
   - 壁厚 2mm 的壳体应该看起来有明显的空腔

3. **特征完整性**:
   - 描述中的孔是否可见？
   - 圆角/倒角是否明显？
   - 壳体是否有开口？
   - 散热筋、安装耳、凸台等是否都在？

4. **实体完整性** (非常重要):
   - 所有子特征是否都与主体物理相连？
   - 是否有游离/分离/悬浮的独立部分？（这是严重错误）
   - 附加特征应从主体表面生长出来，而不是独立悬浮在附近

5. **明显错误**:
   - 零件是否退化为一条线或一个面？
   - 是否有明显的几何撕裂或自交？
   - 是否有不应该存在的突出物？
   - 特征位置是否合理（不在壳体外部游离）？

## 输出 JSON

{
    "is_match": true/false,
    "confidence": 0.0-1.0,
    "issues": [
        "具体问题描述"
    ],
    "suggestions": [
        "修复建议，给出具体 CadQuery 修改方向"
    ]
}

规则:
- confidence < 0.7 时，设置 is_match = false
- 存在分离/游离的独立部分时，必须设 is_match = false，并在 suggestions 中说明哪些部分需要与主体合并
- 关注功能性问题（孔缺失、形状错误、分离实体），忽略美观问题
- 只输出 JSON，不要输出其他文字"""


@dataclass
class VisionValidationResult:
    # True = matches description, False = mismatch (triggers a fix retry),
    # None = INDETERMINATE (could not evaluate — no renders / parse failure).
    # Indeterminate must never read as a pass: it does not flip success, does not
    # trigger a fix, and is surfaced honestly in the inspect report.
    is_match: bool | None
    confidence: float
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class VisionValidator:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not settings.has_llm_credentials:
                raise RuntimeError(settings.llm_credentials_error)
            self._client = make_llm_client()
        return self._client

    async def validate(
        self,
        user_description: str,
        render_paths: list[Path],
        code: str,
    ) -> VisionValidationResult:
        # Encode images as base64
        image_blocks = []
        for path in render_paths:
            if path.exists():
                data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
                image_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{data}"},
                })

        if not image_blocks:
            # No images to judge — INDETERMINATE, not a pass (was is_match=True).
            return VisionValidationResult(
                is_match=None, confidence=0.0,
                issues=["无渲染图可用，视觉校验未执行"], suggestions=[]
            )

        # Build code summary (truncated for context)
        code_summary = ""
        if code:
            code_lines = code.strip().splitlines()
            # Extract parameter section and show_object count
            param_lines = [l for l in code_lines[:30] if "=" in l and not l.strip().startswith("#") and not l.strip().startswith("import")]
            show_count = sum(1 for l in code_lines if "show_object" in l)
            union_count = sum(1 for l in code_lines if ".union(" in l)
            cut_count = sum(1 for l in code_lines if ".cut(" in l)
            code_summary = (
                f"\n\n代码信息: {len(code_lines)} 行, "
                f"show_object调用 {show_count} 次, "
                f"union操作 {union_count} 次, cut操作 {cut_count} 次\n"
                f"参数: {'; '.join(param_lines[:10])}"
            )

        # Build messages
        content = [
            {"type": "text", "text": f"用户描述: {user_description}{code_summary}\n\n以下 4 张图是前/右/顶/等轴测 4 个角度:"},
        ]
        content.extend(image_blocks)
        content.append({"type": "text", "text": "仔细检查所有特征是否与主体相连（不能有分离的部分）。评估是否匹配。输出 JSON。"})

        try:
            response = await self.client.chat.completions.create(
                stream=True,
                model=settings.llm_model,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": VISION_VALIDATION_PROMPT},
                    {"role": "user", "content": content},
                ],
            )

            text = response.choices[0].message.content.strip()
            # Strip markdown fences
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            parsed = json.loads(text)
            return VisionValidationResult(
                is_match=parsed.get("is_match", True),
                confidence=parsed.get("confidence", 0.5),
                issues=parsed.get("issues", []),
                suggestions=parsed.get("suggestions", []),
            )
        except Exception as e:
            logger.warning(f"Vision validation failed: {e}")
            # Parse/LLM failure — INDETERMINATE, not a pass (was is_match=True).
            return VisionValidationResult(
                is_match=None, confidence=0.0,
                issues=["视觉校验解析失败，结果不可信"], suggestions=[]
            )
