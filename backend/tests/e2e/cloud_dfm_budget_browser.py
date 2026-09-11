"""Measured DFM failure after the allowed repair remains an explicit review risk."""
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect
from cloud_document_acceptance import PRIVATE, call, write_review_evidence
from cloud_hole_semantics_browser import run_case


def main():
    out = Path(os.environ["CAD_DFM_BUDGET_REPORT_DIR"])
    private = json.loads(PRIVATE.read_text())
    client = httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]})
    evidence = {}

    def inspect_review(page, task, submitted):
        checks = [v for v in task["agent"]["validations"] if v["gate"] == "dfm"]
        assert len(checks) == 2 and all(v["outcome"] == "failed" for v in checks), checks
        assert task["agent"]["plan"]["validation_policy"]["dfm"]["repair_budget"] == 1
        change = task["change_set"]["id"]
        review = call(client, "GET", "/api/change-sets/" + change)
        summary = review["validation_summary"]
        assert summary["status"] == "warning" and summary["issue_count"] > 0, summary
        call(client, "POST", f"/api/change-sets/{change}/accept", expected=422, json={"note": ""})
        call(client, "POST", f"/api/change-sets/{change}/commit", expected=409)
        page.get_by_role("button", name="查看变更", exact=True).first.click()
        dialog = page.get_by_role("dialog")
        accept = dialog.get_by_role("button", name="接受并确认已审阅风险", exact=True)
        expect(accept).to_be_disabled(timeout=20000)
        expect(dialog).not_to_contain_text("问题 0 项")
        expect(dialog.get_by_role("button", name="提交版本", exact=True)).to_be_disabled()
        dialog.get_by_role("textbox", name="审查意见", exact=True).fill("已阅读薄底制造风险；本次复验不提交此候选。")
        expect(accept).to_be_enabled()
        page.screenshot(path=str(out / "risk-review.png"))
        document = call(client, "GET", "/api/documents/" + submitted["branch_id"])
        assert document["state_version"] == 0 and not document["fcstd"]
        evidence.update(workflow_id=task["id"], dfm_checks=checks, repair_budget=1,
            repair_count=task["agent"]["repair_count"], review_status=summary["status"],
            issue_count=summary["issue_count"], blank_accept_rejected=True,
            direct_commit_rejected=True, risk_note_required_in_browser=True, head_unchanged=True)
        write_review_evidence(out / "review.json", review)

    task, _, artifacts = run_case("dfm_budget_exhausted", {"prompt": (
        "Create a controlled thin-membrane specimen: an 80x60x8 mm rectangular plate "
        "with exactly one centered, flat-bottom, 40 mm diameter blind pocket. "
        "First model the pocket at exactly 7.7 mm depth, leaving a 0.3 mm floor, "
        "and run the actual FDM check. If it flags thin walls, automatically change "
        "the pocket depth to exactly 7.6 mm (floor 0.4 mm), then rerun the checks. "
        "This is the only authorized repair. Never thicken the floor beyond 0.4 mm, "
        "because its thin membrane is the purpose of the specimen. Preserve the "
        "80x60x8 envelope, 40 mm diameter and centered hole position. Do not omit "
        "the pocket or add holes, ribs or fillets. Retain any FDM failure as an "
        "explicit manufacturing risk for human review, never report it as passing. "
        "Export STEP and STL."
    )}, out, on_result=inspect_review)
    (out / "measurement-input.json").write_text(json.dumps([{
        "name": "dfm_budget_exhausted", "dimensions": [80,60,8],
        "holes": [{"x":40,"y":30,"diameter":40,"depth":7.6}], **artifacts}], indent=2))
    (out / "summary.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print("CAD_DFM_BUDGET_BROWSER=" + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
