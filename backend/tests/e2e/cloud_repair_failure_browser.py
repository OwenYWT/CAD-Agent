"""Actual impossible fillet -> bounded provider repairs -> blocked candidate."""
import json
import os
from pathlib import Path

import httpx
from cloud_document_acceptance import PRIVATE, call
from cloud_hole_semantics_browser import run_case


def main():
    out = Path(os.environ["CAD_REPAIR_FAILURE_REPORT_DIR"])
    task, submitted, _ = run_case("impossible_fillet", {"prompt": (
        "Create a solid rectangular plate 80 mm long, 60 mm wide and exactly 8 mm thick. "
        "Apply a native PartDesign fillet to ALL twelve outside edges. Try radius 5 mm first; "
        "if the kernel fails, you may adjust the radius only within the range 4.5 to 5 mm. "
        "The envelope 80x60x8 and minimum radius 4.5 mm are HARD requirements. "
        "Do not omit edges, remove the fillet, increase thickness or lower the radius below "
        "4.5 mm to make the operation succeed. If no valid shape exists, return a failed "
        "task with the actual kernel error after the bounded repair attempts. "
        "No holes. Export STEP and STL only for a valid model satisfying all requirements."
    )}, out, expected_status="failed")
    private = json.loads(PRIVATE.read_text())
    client = httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]})
    document = call(client, "GET", "/api/documents/" + submitted["branch_id"])
    assert document["state_version"] == 0 and not document["fcstd"]
    assert not task["change_set"] and not task["artifacts"], "failure exposed an approvable candidate"
    assert task["agent"]["candidate_status"] == "failed"
    repairs = [s for s in task["steps"] if "repair" in s["kind"]]
    assert repairs, "the invalid model did not exercise automatic repair"
    evidence = {"workflow_id": task["id"], "status": task["status"],
        "error_code": task["error_code"], "error_message": task["error_message"],
        "repair_steps": [{k: s.get(k) for k in ("step_key", "kind", "status")} for s in repairs],
        "head_unchanged": True, "no_approvable_candidate": True}
    (out / "summary.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print("CAD_REPAIR_FAILURE_BROWSER=" + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
