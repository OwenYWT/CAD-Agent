"""Actual visual mismatch remains blocked after one insufficient authorized repair."""
import json
import os
from pathlib import Path

import httpx

from cloud_document_acceptance import PRIVATE, call
from cloud_hole_semantics_browser import run_case
from cloud_visual_fixture import prototype_request


def main():
    out = Path(os.environ["CAD_VISUAL_BUDGET_REPORT_DIR"])
    task, submitted, _ = run_case("visual_budget_exhausted", {
        "prompt": prototype_request(preserve_center=True)}, out, expected_status="failed")
    assert task["error_code"] == "agent_visual_validation_failed", task.get("error_message")
    visuals = [g for g in task["agent"]["validations"] if g["gate"] == "visual"]
    assert len(visuals) == 2 and all(g["outcome"] == "failed" for g in visuals), visuals
    assert task["agent"]["plan"]["validation_policy"]["visual"]["repair_budget"] == 1
    assert task["agent"]["repair_count"] == 1
    assert not task["change_set"] and not task["artifacts"]
    private = json.loads(PRIVATE.read_text())
    with httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]}) as client:
        document = call(client, "GET", "/api/documents/" + submitted["branch_id"])
        assert document["state_version"] == 0 and not document["fcstd"]
    summary = {"workflow_id": task["id"], "status": task["status"], "error_code": task["error_code"],
        "visual_checks": visuals, "visual_repair_budget": 1, "repair_count": 1,
        "head_unchanged": True, "no_approvable_candidate_or_artifacts": True}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print("CAD_VISUAL_BUDGET_BROWSER=" + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
