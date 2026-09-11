"""Actual staged design request exercises the visual repair path without doubles."""
import json
import os
from pathlib import Path

import httpx

from cloud_document_acceptance import PRIVATE, call
from cloud_hole_semantics_browser import run_case
from cloud_visual_fixture import prototype_request


def main():
    out = Path(os.environ["CAD_VISUAL_REPAIR_REPORT_DIR"])
    case = {"prompt": prototype_request(), "dimensions": [80, 60, 8], "holes": [
        {"x": x, "y": y, "diameter": 6, "depth": 8} for x in (10, 70) for y in (10, 50)]}
    task, submitted, _ = run_case("visual_pattern_repair", case, out)
    repairs = [s for s in task["steps"] if s["step_key"].startswith("visual-repair-")]
    assert repairs, "the request did not exercise the visual repair branch"
    visuals = [g for g in task["agent"]["validations"] if g["gate"] == "visual"]
    assert any(g["outcome"] == "failed" for g in visuals), "no actual visual mismatch was recorded"
    private = json.loads(PRIVATE.read_text())
    with httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]}) as client:
        review = call(client, "GET", "/api/change-sets/" + task["change_set"]["id"])
        assert review["validation_summary"]["status"] == "passed", review["validation_summary"]
        assert all(g["outcome"] == "passed" for g in review["validation_summary"]["gates"])
        document = call(client, "GET", "/api/documents/" + submitted["branch_id"])
        assert document["state_version"] == 0 and not document["fcstd"]
    summary = {"workflow_id": task["id"], "visual_checks": visuals,
        "repair_steps": [{k: s[k] for k in ("step_key", "kind", "status")} for s in repairs],
        "final_review_summary": review["validation_summary"], "head_unchanged_before_review": True}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print("CAD_VISUAL_REPAIR_BROWSER=" + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
