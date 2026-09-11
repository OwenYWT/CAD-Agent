"""Read committed CAD and engineering evidence after real container recreation."""
import hashlib
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ["CAD_NATIVE_E2E_URL"].rstrip("/")
    private = json.loads(Path(os.environ["CAD_NATIVE_E2E_PRIVATE"]).read_text())
    evidence = Path(os.environ["CAD_DEPLOY_EVIDENCE"])
    before = private["committed"]
    client = httpx.Client(base_url=base, timeout=60, transport=httpx.HTTPTransport(retries=3),
                         headers={"Authorization": "Bearer " + private["owner"]["token"]})
    ready = client.get("/ready"); ready.raise_for_status()
    response = client.get("/api/documents/" + private["document_id"]); response.raise_for_status()
    after = response.json()
    for key in ("head_revision_id", "state_version", "parameter_state_sha256"):
        assert before[key] == after[key]
    for kind in ("fcstd", "mesh", "state"):
        assert before[kind]["sha256"] == after[kind]["sha256"]
        r = client.get(after[kind]["url"]); r.raise_for_status()
        assert hashlib.sha256(r.content).hexdigest() == before[kind]["sha256"]
    results = {}
    for kind, filename in (("fea", "cad-expansion-engineering-http.json"), ("cam", "cad-expansion-cam-browser.json")):
        prior = json.loads((evidence / filename).read_text())
        r = client.get(f"/api/documents/{private['document_id']}/engineering/{prior['workflow_run_id']}")
        r.raise_for_status(); current = r.json()
        assert current["source_revision_id"] == before["head_revision_id"]
        for artifact in current["artifacts"].values():
            data = client.get(artifact["url"]); data.raise_for_status()
            assert hashlib.sha256(data.content).hexdigest() == artifact["sha256"]
        results[kind] = {"workflow_id": prior["workflow_run_id"], "source_revision_unchanged": True,
                         "artifact_hashes_verified": len(current["artifacts"])}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(base + "/")
        page.locator('input[type="text"]').fill(private["owner"]["phone"])
        page.locator('input[type="password"]').fill(private["owner"]["password"])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role("button", name="新建设计", exact=True)).to_be_visible(timeout=30000)
        page.goto(base + "/?document=" + private["document_id"] + "&workspace=" + private["tenant_id"])
        panel = page.get_by_test_id("cloud-document-panel")
        panel.get_by_role("treeitem", name="HoleSketch", exact=True).click()
        expect(page.get_by_test_id("sketch-editor").get_by_role("spinbutton", name="草图约束 2 Diameter", exact=True)).to_have_value("9", timeout=30000)
        expect(page.get_by_test_id("document-scene").locator("canvas")).to_be_visible(timeout=30000)
        page.screenshot(path=str(evidence / "after-recreate-browser.png"))
        browser.close()
    report = {"revision": after["head_revision_id"], "state_version": after["state_version"],
              "fcstd_sha256": after["fcstd"]["sha256"], "committed_artifacts_identical": True,
              "password_login_and_browser_restore": True, "diameter_mm": 9, "engineering": results,
              "ready": ready.json()}
    (evidence / "after-recreate-verification.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    client.close()


if __name__ == "__main__":
    main()
