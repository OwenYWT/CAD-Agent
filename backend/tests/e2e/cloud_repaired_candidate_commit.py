"""Review an independently measured repair and verify the committed bytes."""
import hashlib
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from cloud_document_acceptance import BASE, PRIVATE, accept_in_browser, call


def main():
    out = Path(os.environ["CAD_REPAIR_REPORT_DIR"])
    submitted = json.loads((out / "browser.json").read_text())
    private = json.loads(PRIVATE.read_text())
    client = httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]})
    path = "/api/documents/" + submitted["branch_id"]
    before = call(client, "GET", path)
    assert before["state_version"] == 0 and not before["fcstd"]
    task = call(client, "GET", "/api/tasks/" + submitted["workflow_run_id"] + "/snapshot")
    assert task["status"] == "succeeded" and task["agent"]["repair_count"] >= 1
    review = call(client, "GET", "/api/change-sets/" + task["change_set"]["id"])
    assert review["validation_summary"]["status"] == "passed"
    assert all(g["outcome"] == "passed" for g in review["validation_summary"]["gates"])
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
        page.goto(web + "?document=" + submitted["branch_id"] + "&workspace=" + private["tenant_id"])
        review_button = page.get_by_role("button", name="查看变更", exact=True).first
        expect(review_button).to_be_visible(timeout=20000)
        review_button.click()
        dialog = page.get_by_role("dialog")
        accept_in_browser(dialog, "已核对修复前后检查记录和最终工件的独立几何测量，接受本次修复。")
        commit_button = dialog.get_by_role("button", name="提交版本", exact=True)
        expect(commit_button).to_be_enabled(timeout=15000)
        commit_button.click()
        expect(dialog.get_by_text("审查状态：committed", exact=True)).to_be_visible(timeout=20000)
        dialog.get_by_role("button", name="关闭", exact=True).last.click()
        after = call(client, "GET", path)
        assert after["state_version"] == 1 and after["head_revision_id"] != before["head_revision_id"]
        scene = page.get_by_test_id("document-scene")
        expect(scene).to_have_attribute("data-revision", after["head_revision_id"], timeout=30000)
        assert scene.locator("canvas").evaluate("c => !!c.getContext('webgl2')")
        expected_sha = hashlib.sha256((out / "model.fcstd").read_bytes()).hexdigest()
        response = client.get(BASE + after["fcstd"]["url"])
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == after["fcstd"]["sha256"] == expected_sha
        assert all(f["revision_created"] == after["head_revision_id"] for f in after["features"])
        page.screenshot(path=str(out / "committed.png"))
        assert not errors, errors
        browser.close()
    evidence = {"workflow_id": task["id"], "document_id": submitted["branch_id"],
                "revision_id": after["head_revision_id"], "state_version": after["state_version"],
                "browser_review_and_commit": True, "rendered_committed_revision": True,
                "committed_fcstd_sha256": expected_sha,
                "committed_bytes_equal_independently_measured_repair": True, "page_errors": errors}
    (out / "commit-summary.json").write_text(json.dumps(evidence, indent=2))
    print("CAD_REPAIR_COMMIT=" + json.dumps(evidence), flush=True)
    client.close()


if __name__ == "__main__":
    main()
