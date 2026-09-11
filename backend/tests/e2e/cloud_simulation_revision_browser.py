"""A completed real solve must not validate a subsequently edited revision."""
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from cloud_document_acceptance import PRIVATE, call, commit, wait_task


def main():
    private = json.loads(PRIVATE.read_text())
    client = httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]})
    out = Path(os.environ["CAD_SIMULATION_REVISION_REPORT_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    previous = json.loads(Path("/tmp/cad-expansion-engineering-browser.json").read_text())
    path = "/api/documents/" + private["document_id"]
    before = call(client, "GET", path)
    result = call(client, "GET", path + "/engineering/" + previous["workflow_run_id"])
    assert result["source_revision_id"] == before["head_revision_id"]
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(os.environ["CAD_NATIVE_E2E_WEB"])
        page.locator('input[type="text"]').fill(private["owner"]["phone"])
        page.locator('input[type="password"]').fill(private["owner"]["password"])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role("button", name="新建设计", exact=True)).to_be_visible(timeout=20000)
        page.get_by_role("button", name="Create a rectangular plate", exact=False).first.click()
        page.get_by_role("button", name="返回项目流程：", exact=False).click()
        stage = page.get_by_role("heading", name="仿真验证", exact=True).locator("xpath=../../..")
        expect(stage).to_contain_text("已完成", timeout=20000)
        hole = next(f for f in before["features"] if f["type"] == "PartDesign::Hole")
        diameter = next(p for p in hole["parameters"] if p["property_name"] == "Diameter")
        submitted = call(client, "POST", path + "/operations", expected=202, json={
            "action": "parameters.update", "expected_base_revision_id": before["head_revision_id"],
            "expected_state_version": before["state_version"], "idempotency_key": str(uuid4()),
            "modification": {"expected_state_sha256": before["parameter_state_sha256"],
                "parameter_updates": [{"parameter_id": diameter["id"], "value": diameter["value"] + 0.25}]}})
        commit(client, wait_task(client, submitted["workflow_run_id"]))
        after = call(client, "GET", path)
        assert after["head_revision_id"] != before["head_revision_id"]
        expect(stage).not_to_contain_text("已完成", timeout=20000)
        page.screenshot(path=str(out / "new-revision-needs-solve.png"))
        page.get_by_role("button", name="结构仿真", exact=True).click()
        panel = page.locator('section[aria-label="结构仿真工作台"]').get_by_test_id("engineering-tasks")
        panel.locator(f'[data-engineering-task="{previous["workflow_run_id"]}"]').get_by_role("button", name="查看计算结果").click()
        rendered = panel.get_by_test_id("engineering-result")
        expect(rendered).to_have_attribute("data-revision", before["head_revision_id"], timeout=20000)
        expect(rendered).to_contain_text("历史修订")
        expect(rendered.get_by_test_id("engineering-field-viewer").locator("canvas")).to_be_visible()
        page.screenshot(path=str(out / "historical-result.png"))
        assert not errors, errors
        browser.close()
    evidence = {"analysis_workflow_id": previous["workflow_run_id"], "edit_workflow_id": submitted["workflow_run_id"],
        "old_revision": before["head_revision_id"], "new_revision": after["head_revision_id"],
        "new_revision_does_not_inherit_pass": True, "old_result_marked_historical": True, "page_errors": errors}
    (out / "summary.json").write_text(json.dumps(evidence, indent=2))
    print("CAD_SIMULATION_REVISION_BROWSER=" + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
