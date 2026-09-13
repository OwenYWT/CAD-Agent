"""Pure gate policy shared by workflow, candidate sealing and review.

Repair happens before this decision. Advisory uncertainty may be reviewed with
a note; a measured visual mismatch must never become an applicable candidate.
The legacy workflow switch only preserves pre-patch Temporal command histories.
"""
from typing import Literal


def gate_blocks(gate: str, mode: str, outcome: str | None, *,
                stage: Literal["workflow", "seal", "review"] = "review",
                legacy_visual_workflow: bool = False) -> bool:
    if gate not in {"geometry", "visual", "dfm", "bom"}:
        raise ValueError(f"unsupported gate: {gate}")
    if mode not in {"required", "advisory", "disabled"}:
        raise ValueError(f"unsupported gate mode: {mode}")
    if outcome not in {None, "passed", "failed", "indeterminate"}:
        raise ValueError(f"unsupported gate outcome: {outcome}")
    if stage not in {"workflow", "seal", "review"}:
        raise ValueError(f"unsupported gate stage: {stage}")
    if mode == "disabled":
        return outcome is not None
    # An enabled gate must supply evidence, including advisory uncertainty.
    if outcome is None:
        return True
    return (mode == "required" and outcome != "passed") or (
        gate == "visual" and outcome == "failed"
        and not (stage == "workflow" and legacy_visual_workflow)
    )
