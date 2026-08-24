"""The vision boundary: renders plus measured facts in, structured critique out.

This is the only place in the package that talks to a model provider. It takes
an already-built client so the loop, the tests and the durable activities can
all inject their own, and it never invents a verdict: any provider or parse
failure returns an explicitly indeterminate critique.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any, Callable, Sequence

from app.visual_refine.contracts import RequirementCheck, VisualCritique
from app.visual_refine.facts import GeometryFacts
from app.visual_refine.prompts import (
    CRITIC_SYSTEM_PROMPT,
    critic_closing_text,
    critic_user_text,
)
from app.visual_refine.spec import DesignSpec

logger = logging.getLogger(__name__)

_MAX_IMAGES = 6
_MAX_IMAGE_BYTES = 6 * 1024 * 1024


def encode_image(path: Path) -> dict[str, Any]:
    """Encode one PNG as an OpenAI-compatible image part."""
    payload = path.read_bytes()
    if len(payload) > _MAX_IMAGE_BYTES:
        raise ValueError(f"render is too large to send: {path.name}")
    encoded = base64.b64encode(payload).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{encoded}"},
    }


def parse_critique_payload(
    raw: str,
    *,
    spec: DesignSpec,
    measured_checks: Sequence[RequirementCheck] = (),
) -> VisualCritique:
    """Turn a provider response into a critique, keeping measurement on top.

    Unknown keys the model invented are dropped and requested keys it skipped
    become ``not_visible``: the check set is decided by the spec, not by
    whatever the model felt like answering, so the score stays comparable
    between candidates and between runs.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("vision response was not a JSON object")

    wanted = {item.key: item for item in spec.requirements if not item.measurable}
    reported: dict[str, RequirementCheck] = {}
    for entry in parsed.get("checks") or ():
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        requirement = wanted.get(key)
        if requirement is None:
            continue  # the model invented a key; the spec decides the check set
        verdict = str(entry.get("verdict") or "").strip().lower()
        if verdict not in {"satisfied", "violated", "not_visible"}:
            verdict = "not_visible"
        confidence = entry.get("confidence")
        try:
            confidence_value = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            confidence_value = 0.5
        reported[key] = RequirementCheck(
            key=key,
            requirement=requirement.text,
            verdict=verdict,  # type: ignore[arg-type]
            observed=str(entry.get("observed") or "")[:800],
            severity=requirement.severity,
            origin="observed",
            confidence=confidence_value,
        )

    for key, requirement in wanted.items():
        if key not in reported:
            reported[key] = RequirementCheck(
                key=key,
                requirement=requirement.text,
                verdict="not_visible",
                observed="the inspector did not report on this requirement",
                severity=requirement.severity,
                origin="observed",
                confidence=0.0,
            )

    # Measured checks come last so they are never shadowed by an observation of
    # the same key, and their failures dominate the repair brief.
    checks = tuple(measured_checks) + tuple(
        reported[key] for key in wanted if key in reported
    )

    issues = tuple(
        str(item)[:600] for item in (parsed.get("issues") or ()) if str(item).strip()
    )
    suggestions = tuple(
        str(item)[:600]
        for item in (parsed.get("suggestions") or ())
        if str(item).strip()
    )
    try:
        overall_confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.0))))
    except (TypeError, ValueError):
        overall_confidence = 0.0

    return VisualCritique(
        checks=checks,
        issues=issues,
        suggestions=suggestions,
        summary=str(parsed.get("summary") or "")[:2000],
        confidence=overall_confidence,
    )


def indeterminate(reason: str, measured_checks: Sequence[RequirementCheck] = ()) -> VisualCritique:
    """An honest 'could not judge'. Never a pass, never a fail."""
    return VisualCritique(
        checks=tuple(measured_checks),
        indeterminate=True,
        indeterminate_reason=reason[:500] or "vision critique unavailable",
    )


class VisionCritic:
    """Default critic backed by an OpenAI-compatible chat completions client."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.1,
        provenance_reader: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._provenance_reader = provenance_reader
        self.last_provenance: dict[str, Any] | None = None

    async def critique(
        self,
        *,
        spec: DesignSpec,
        render_paths: Sequence[Path],
        facts: GeometryFacts | None = None,
        source_code: str = "",
        measured_checks: Sequence[RequirementCheck] = (),
    ) -> VisualCritique:
        """Judge the renders. Returns indeterminate rather than guessing."""
        usable = [path for path in render_paths if Path(path).is_file()][:_MAX_IMAGES]
        if not usable:
            return indeterminate("no renders were produced", measured_checks)
        try:
            return await self.judge(
                spec=spec,
                render_paths=usable,
                facts=facts,
                measured_checks=measured_checks,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("visual critique unavailable: %s: %s", type(exc).__name__, exc)
            return indeterminate(
                f"vision_provider_unavailable:{type(exc).__name__}", measured_checks
            )

    async def judge(
        self,
        *,
        spec: DesignSpec,
        render_paths: Sequence[Path],
        facts: GeometryFacts | None = None,
        measured_checks: Sequence[RequirementCheck] = (),
    ) -> VisualCritique:
        """Ask the vision model and parse its answer, raising on any failure.

        ``critique`` wraps this and converts a failure into an indeterminate
        result. Callers that need to distinguish "provider was unreachable" from
        "model judged it a fail" -- the durable gate does -- call this directly.
        """
        measured_briefing = _format_measured(measured_checks)
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": critic_user_text(
                    spec_briefing=spec.briefing(),
                    facts_briefing=(
                        facts.render_briefing() if facts is not None else ""
                    ),
                    measured_briefing=measured_briefing,
                ),
            }
        ]
        for path in render_paths:
            content.append(encode_image(Path(path)))
        content.append(
            {
                "type": "text",
                "text": critic_closing_text(
                    view_order=", ".join(Path(path).stem for path in render_paths),
                    keys=", ".join(spec.visual_requirement_keys()),
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
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        choices = list(getattr(response, "choices", ()) or ())
        if not choices:
            raise ValueError("vision provider returned no choice")
        critique = parse_critique_payload(
            str(choices[0].message.content or ""),
            spec=spec,
            measured_checks=measured_checks,
        )
        if self._provenance_reader is not None:
            self.last_provenance = self._provenance_reader()
        return critique


def _format_measured(checks: Sequence[RequirementCheck]) -> str:
    if not checks:
        return ""
    lines = ["ALREADY MEASURED (authoritative, do not re-judge from the image):"]
    for check in checks:
        mark = {
            "satisfied": "OK",
            "violated": "FAIL",
            "not_visible": "n/a",
        }.get(check.verdict, "n/a")
        lines.append(f"- [{mark}] {check.requirement} -> {check.observed}")
    return "\n".join(lines)
