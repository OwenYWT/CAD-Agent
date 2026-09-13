"""An exact invalid radius fails in real FreeCAD without provider redesign.

Run after cloud_agent_inspection_acceptance against the same isolated deployment.
The existing valid fillet and all committed artifact bytes must remain unchanged.
"""
import hashlib
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from cloud_document_acceptance import BASE, PRIVATE, call, wait_task


def submit_and_verify_browser(private, source, fillet, radius, client, out):
    path = "/api/documents/" + source["document_id"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        web = os.environ["CAD_NATIVE_E2E_WEB"]
        page.goto(web)
        page.locator('input[type="text"]').fill(private["owner"]["phone"])
        page.locator('input[type="password"]').fill(private["owner"]["password"])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role("button", name="新建设计", exact=True)).to_be_visible(timeout=20000)
        page.goto(web + "?document=" + source["document_id"] + "&workspace=" + private["tenant_id"])
        panel = page.get_by_test_id("cloud-document-panel")
        panel.get_by_role("treeitem", name=fillet["label"], exact=True).click()
        field = panel.get_by_role("spinbutton", name=radius["id"], exact=True)
        expect(field).to_have_value("0.5", timeout=15000)
        field.fill("4.5")
        with page.expect_response(lambda r: r.url.endswith(path + "/operations") and r.request.method == "POST") as response:
            panel.get_by_role("button", name="提交参数变更", exact=True).click()
        assert response.value.status == 202, response.value.text()
        task = wait_task(client, response.value.json()["workflow_run_id"])
        (out / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2))
        assert task["status"] == "failed", (task["status"], task.get("error_message"))
        task_panel = page.get_by_role("region", name="文档任务", exact=True)
        expect(task_panel.get_by_role("alert")).to_contain_text("原始检查：", timeout=15000)
        expect(field).to_have_value("4.5")
        page.screenshot(path=str(out / "failure-visible.png"))
        page.reload()
        panel.get_by_role("treeitem", name=fillet["label"], exact=True).click()
        expect(field).to_have_value("0.5", timeout=15000)
        page.screenshot(path=str(out / "committed-radius-preserved.png"))
        assert not errors, errors
        browser.close()
        return task


def main():
    private = json.loads(PRIVATE.read_text())
    source = json.loads(Path(os.environ["CAD_EXACT_PARAMETER_SOURCE"]).read_text())
    out = Path(os.environ["CAD_EXACT_PARAMETER_REPORT_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]}) as client:
        path = "/api/documents/" + source["document_id"]
        before = call(client, "GET", path)
        fillet = next(f for f in before["features"] if f["type"] == "PartDesign::Fillet")
        radius = next(p for p in fillet["parameters"] if p["property_name"] == "Radius")
        assert radius["value"] == 0.5, "use the existing valid inspection fixture"
        task = submit_and_verify_browser(private, source, fillet, radius, client, out)
        assert task["error_code"] == "native_edit_repair_forbidden", task.get("error_message")
        assert "原始检查：" in task["error_message"]
        assert not task["change_set"] and not task["artifacts"]
        assert task["agent"]["repair_count"] == 0
        after = call(client, "GET", path)
        for key in ("head_revision_id", "state_version", "parameter_state_sha256", "features"):
            assert after[key] == before[key], key
        hashes = {}
        for kind in ("fcstd", "state", "mesh"):
            assert before[kind]["sha256"] == after[kind]["sha256"], kind
            response = client.get(BASE + after[kind]["url"])
            assert response.status_code == 200
            hashes[kind] = hashlib.sha256(response.content).hexdigest()
            assert hashes[kind] == before[kind]["sha256"]
        result = {"workflow_id": task["id"], "document_id": source["document_id"],
                  "status": task["status"], "error_code": task["error_code"],
                  "error_message": task["error_message"], "requested_radius_mm": 4.5,
                  "preserved_radius_mm": radius["value"], "repair_count": 0,
                  "browser_submission_and_failure_visible": True,
                  "failed_exact_input_retained_until_reload": True,
                  "browser_reload_preserves_committed_radius": True,
                  "head_unchanged": True, "no_candidate_or_partial_artifact": True,
                  "committed_artifact_hashes": hashes}
        (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print("CAD_EXACT_PARAMETER_FAILURE=" + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
