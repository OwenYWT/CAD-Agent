"""Vision-guided source repair: the model fixes the code while looking at it.

The repair path that existed before this package described defects to the model
in words and asked it to fix code it had never seen the output of. That is the
CAD equivalent of fixing a layout from a bug report instead of a screenshot --
and it is exactly the gap this closes: the renders of the failing build go into
the repair request alongside the source.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Sequence

from app.visual_refine.contracts import VisualCritique
from app.visual_refine.critic import encode_image
from app.visual_refine.facts import GeometryFacts
from app.visual_refine.prompts import PATCHER_SYSTEM_PROMPT, patcher_user_text
from app.visual_refine.spec import DesignSpec

logger = logging.getLogger(__name__)

_MAX_IMAGES = 5


def extract_code(text: str) -> str:
    """Pull runnable Python out of a model response, fenced or not."""
    body = (text or "").strip()
    if "```python" in body:
        start = body.index("```python") + len("```python")
        end = body.find("```", start)
        return (body[start:] if end == -1 else body[start:end]).strip()
    if "```" in body:
        start = body.index("```") + 3
        newline = body.find("\n", start)
        if newline != -1:
            start = newline + 1
        end = body.find("```", start)
        return (body[start:] if end == -1 else body[start:end]).strip()
    return body


class VisionPatcher:
    """Rewrites CadQuery source from a critique plus the renders that produced it."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        max_tokens: int = 8192,
        temperature: float = 0.1,
        provenance_reader: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._provenance_reader = provenance_reader
        self.last_provenance: dict[str, Any] | None = None

    async def patch(
        self,
        *,
        source_code: str,
        critique: VisualCritique,
        spec: DesignSpec,
        render_paths: Sequence[Path] = (),
        facts: GeometryFacts | None = None,
    ) -> str:
        defects = critique.repair_brief()
        if not defects.strip():
            raise ValueError("nothing to repair: the critique lists no defect")

        if critique.suggestions:
            defects += "\n\nInspector suggestions:\n" + "\n".join(
                f"- {item}" for item in critique.suggestions
            )

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": patcher_user_text(
                    objective=spec.objective,
                    defects=defects,
                    facts_briefing=(
                        facts.render_briefing() if facts is not None else ""
                    ),
                    source_code=source_code,
                ),
            }
        ]
        usable = [path for path in render_paths if Path(path).is_file()][:_MAX_IMAGES]
        for path in usable:
            content.append(encode_image(Path(path)))
        if usable:
            content.append(
                {
                    "type": "text",
                    "text": (
                        "Those renders are what the current source actually "
                        f"produced ({', '.join(Path(p).stem for p in usable)}). "
                        "Return the corrected complete source now."
                    ),
                }
            )

        if self._provenance_reader is not None:
            from app.llm import reset_chat_completion_provenance

            reset_chat_completion_provenance()

        response = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": PATCHER_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        choices = list(getattr(response, "choices", ()) or ())
        if not choices:
            raise ValueError("repair provider returned no choice")
        repaired = extract_code(str(choices[0].message.content or ""))
        if not repaired.strip():
            raise ValueError("visual repair returned empty source")
        if repaired.strip() == source_code.strip():
            raise ValueError("visual repair returned unchanged source")
        if self._provenance_reader is not None:
            self.last_provenance = self._provenance_reader()
        return repaired
