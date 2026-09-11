"""Actual browser/provider/kernel DFM repair; no substituted gate results."""
import json
import os
from pathlib import Path

import httpx

from cloud_document_acceptance import PRIVATE, call, write_review_evidence
from cloud_hole_semantics_browser import run_case


def main():
    out = Path(os.environ["CAD_REPAIR_REPORT_DIR"])
    case = {
        "prompt": (
            "Create an 80 mm long, 60 mm wide, 8 mm thick rectangular plate. "
            "Cut one flat-bottom cylindrical BLIND pocket, diameter exactly 40 mm, "
            "centered on the plate at (40,30) measured from its lower left corner. "
            "For the first modeled candidate use pocket depth 7.7 mm (floor 0.3 mm), "
            "then run the actual FDM manufacturing check. If that measured check flags "
            "the floor as too thin, you are explicitly authorized to automatically "
            "reduce only the pocket depth, making the floor between 1 and 2 mm thick, "
            "and run all checks again. The final pocket depth may therefore be 6 to "
            "7.7 mm; the preferred starting depth is not a hard final constraint. "
            "Do not change the 80x60x8 envelope, pocket count, diameter or center. "
            "No fillets or chamfers. Export STEP and STL."
        ),
    }
    task, submitted, artifacts = run_case("dfm_floor_repair", case, out)
    verify(task, submitted, artifacts, out)


def verify(task, submitted, artifacts, out):
    private = json.loads(PRIVATE.read_text())
    client = httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]})
    events, cursor = [], 0
    for _ in range(20):
        page = call(client, "GET", f"/api/tasks/{task['id']}/events?after_sequence={cursor}&limit=500")
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    else:
        raise AssertionError("unbounded event history")
    (out / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
    assert task["agent"]["repair_count"] >= 1, "no real automatic repair occurred"
    dfm_steps = [s for s in task["steps"] if s["kind"] == "agent_dfm_validation"]
    assert len(dfm_steps) >= 2, "DFM was not rerun after repair"
    assert any(s["step_key"].startswith("dfm-repair-") for s in task["steps"]), "no DFM repair execution"
    # Snapshot validations preserve failed historical attempts. Only evidence
    # sealed with the final artifact may certify that artifact for review.
    review = call(client, "GET", "/api/change-sets/" + task["change_set"]["id"])
    write_review_evidence(out / "review.json", review)
    final_gates = review["validation_summary"]["gates"]
    assert review["validation_summary"]["status"] == "passed", review["validation_summary"]
    assert all(v["outcome"] == "passed" for v in final_gates), final_gates
    assert any(v["gate"] == "dfm" and v["outcome"] == "failed" for v in task["agent"]["validations"])
    document = call(client, "GET", "/api/documents/" + submitted["branch_id"])
    assert document["state_version"] == 0, "candidate generation advanced the branch before review"
    evidence = {"workflow_id": task["id"], "repair_count": task["agent"]["repair_count"],
                "dfm_check_count": len(dfm_steps), "final_gates": final_gates,
                "head_unchanged_before_review": True, "artifacts": artifacts}
    (out / "summary.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print("CAD_DFM_REPAIR_BROWSER=" + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
