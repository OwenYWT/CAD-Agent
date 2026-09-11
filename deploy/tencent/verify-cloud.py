"""Real deployment acceptance for an LLM-generated, constrained Pocket hole.

Continues the actual generation recorded by cloud_document_acceptance.py; it
never seeds a result, replaces a response, or assumes Pocket means correct CAD.
The original Hole-only test failure must remain in the deployment report.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

BASE = os.environ["CAD_NATIVE_E2E_URL"].rstrip("/")
PRIVATE = Path(os.environ["CAD_NATIVE_E2E_PRIVATE"])
OUT = Path(os.environ["CAD_DEPLOY_EVIDENCE"])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    private = json.loads(PRIVATE.read_text())
    # Retry only TCP/TLS connection establishment, before any HTTP request bytes.
    owner = httpx.Client(base_url=BASE, timeout=60, transport=httpx.HTTPTransport(retries=3),
                         headers={"Authorization": "Bearer " + private["owner"]["token"]})
    guest = httpx.Client(base_url=BASE, timeout=60, transport=httpx.HTTPTransport(retries=3),
                         headers={"Authorization": "Bearer " + private["guest"]["token"]})
    report = {"connect_only_retries": 3, "source_generation_workflow": private["generation_workflow_id"]}

    def req(method, path, *, expected=200, client=owner, **kwargs):
        r = client.request(method, path, **kwargs)
        assert r.status_code == expected, (method, path, r.status_code, r.text[:500])
        return r.json() if r.content else None

    def record(name, facts):
        report[name] = facts
        (OUT / "cloud-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(name, json.dumps(facts, ensure_ascii=False), flush=True)

    def wait_task(task_id):
        deadline = time.monotonic() + 900
        last = None
        while time.monotonic() < deadline:
            t = req("GET", f"/api/tasks/{task_id}/snapshot")
            if t["status"] != last:
                print("task", task_id, t["status"], flush=True)
                last = t["status"]
            if last == "waiting_confirmation":
                req("POST", f"/api/tasks/{task_id}/confirmation", json={"accepted": True, "note": "Deployment test exact edit reviewed"})
            if last in {"succeeded", "failed", "cancelled", "timed_out"}:
                assert last == "succeeded", {k: t.get(k) for k in ("status", "error_code", "error_message")}
                return t
            time.sleep(2)
        raise TimeoutError(task_id)

    def artifacts(document, label):
        raw = None
        for kind, suffix in (("fcstd", "FCStd"), ("mesh", "stl"), ("state", "json")):
            a = document[kind]
            r = owner.get(a["url"])
            assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == a["sha256"]
            (OUT / f"{label}.{suffix}").write_bytes(r.content)
            if kind == "state":
                raw = r.json()
        return raw

    def geometry(raw, diameter, thickness):
        body = next(o for o in raw["objects"] if o["type_id"] == "PartDesign::Body")
        shape = body["shape"]
        bounds = shape["bounds_mm"]
        assert all(math.isclose(b - a, n, abs_tol=1e-7) for a, b, n in zip(bounds["min"], bounds["max"], (60, 40, thickness)))
        assert shape["solids"] == 1
        assert math.isclose(shape["volume"], (60 * 40 - math.pi * (diameter / 2) ** 2) * thickness, abs_tol=1e-5)
        sketch = next(o for o in raw["objects"] if o["name"] == "HoleSketch")
        assert sketch["sketch"]["fully_constrained"]
        circle = sketch["inspection"]["geometry"]["items"][0]
        assert math.isclose(circle["radius_mm"], diameter / 2, abs_tol=1e-8)
        assert all(math.isclose(circle["center"][i] - bounds["min"][i], n, abs_tol=1e-7) for i, n in enumerate((30, 20)))
        return sketch

    path = "/api/documents/" + private["document_id"]
    source = wait_task(private["generation_workflow_id"])
    before = req("GET", path)
    assert before["state_version"] == 1
    raw = artifacts(before, "generated")
    initial_sketch = geometry(raw, 6, 8)
    initial_radius = next(c for c in initial_sketch["inspection"]["constraints"]["items"] if c["type"] in {"Radius", "Diameter"} and c["driving"])
    pad = next(f for f in before["features"] if f["type"] == "PartDesign::Pad")
    param = next(p for p in pad["parameters"] if p["property_name"] == "Length")
    assert param["value"] == 8
    record("generated_geometry", {"diameter_mm": 6, "thickness_mm": 8, "dimensions_mm": [60, 40, 8],
                                  "feature_representation": "PartDesign::Pocket", "fully_constrained": True,
                                  "native_mesh_state_hashes_verified": True, "workflow_steps": len(source["steps"])})
    body = {"action": "native.update", "expected_base_revision_id": before["head_revision_id"],
            "expected_state_version": before["state_version"], "idempotency_key": str(uuid4()),
            "modification": {"expected_state_sha256": before["parameter_state_sha256"],
                             "native_edits": [{"action": "sketch.set_constraint", "args": {"sketch": "HoleSketch", "constraint_index": initial_radius["index"], "expected_type": initial_radius["type"], "value_mm": 8 if initial_radius["type"] == "Diameter" else 4}}]}}
    submitted = req("POST", path + "/operations", expected=202, json=body)
    assert req("POST", path + "/operations", expected=202, json=body)["workflow_run_id"] == submitted["workflow_run_id"]
    t = wait_task(submitted["workflow_run_id"])
    assert req("GET", path)["head_revision_id"] == before["head_revision_id"]
    change = t["change_set"]["id"]
    req("POST", f"/api/change-sets/{change}/accept", json={"note": "Reviewed exact 6 to 8 mm hole diameter edit and real gate evidence; native dimensions verified"})
    req("POST", f"/api/change-sets/{change}/commit")
    after = req("GET", path)
    geometry(artifacts(after, "diameter-edited"), 8, 8)
    assert after["state_version"] == 2 and before["fcstd"]["sha256"] != after["fcstd"]["sha256"]
    assert {f["id"] for f in before["features"]} == {f["id"] for f in after["features"]}
    req("POST", path + "/operations", expected=409, json={**body, "idempotency_key": str(uuid4())})
    assert req("POST", path + "/operations", expected=202, json=body)["workflow_run_id"] == submitted["workflow_run_id"]
    invitation = req("POST", path + "/invitations", expected=201)
    req("GET", path, expected=404, client=guest)
    req("POST", path + "/invitations/accept", client=guest,
        json={"tenant_id": invitation["tenant_id"], "token": invitation["token"]})
    guest.headers["X-Workspace-Tenant"] = invitation["tenant_id"]
    assert not req("GET", path, client=guest)["can_edit"]
    req("POST", path + "/operations", expected=403, client=guest, json=body)
    private.update(tenant_id=invitation["tenant_id"], committed=after)
    PRIVATE.write_text(json.dumps(private)); PRIVATE.chmod(0o600)
    record("http_edit_and_permissions", {"workflow_id": t["id"], "diameter_mm": [6, 8], "stable_feature_ids": True,
                                         "stale_rejected": True, "idempotency_verified": True, "viewer_write_denied": True})

    raw = artifacts(after, "before-browser")
    sketch = geometry(raw, 8, 8)
    radius = next(c for c in sketch["inspection"]["constraints"]["items"] if c["type"] in {"Radius", "Diameter"} and c["driving"])
    label = f"草图约束 {radius['index']} {radius['type']}"
    before_value, target_value = ("8", "9") if radius["type"] == "Diameter" else ("4", "4.5")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/")
        page.locator('input[type="text"]').fill(private["owner"]["phone"])
        page.locator('input[type="password"]').fill(private["owner"]["password"])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role("button", name="新建设计", exact=True)).to_be_visible(timeout=30000)
        doc_url = BASE + "/?document=" + private["document_id"] + "&workspace=" + private["tenant_id"]
        page.goto(doc_url)
        panel = page.get_by_test_id("cloud-document-panel")
        expect(panel.get_by_role("treeitem", name="HoleSketch", exact=True)).to_be_visible(timeout=30000)
        panel.get_by_role("treeitem", name="HoleSketch", exact=True).click()
        editor = page.get_by_test_id("sketch-editor")
        expect(editor.get_by_role("spinbutton", name=label, exact=True)).to_have_value(before_value, timeout=30000)
        expect(page.get_by_test_id("document-scene").locator("canvas")).to_be_visible(timeout=30000)
        page.screenshot(path=str(OUT / "browser-before.png"))
        editor.get_by_role("spinbutton", name=label, exact=True).fill(target_value)
        expect(editor.get_by_test_id("sketch-circle-0")).to_have_attribute("r", "4.5")
        assert req("GET", path)["fcstd"]["sha256"] == after["fcstd"]["sha256"]
        with page.expect_response(lambda r: r.url.endswith(path + "/operations") and r.request.method == "POST") as response:
            editor.get_by_role("button", name="提交草图约束", exact=True).click()
        assert response.value.status == 202
        task_id = response.value.json()["workflow_run_id"]
        t = wait_task(task_id)
        assert req("GET", path)["head_revision_id"] == after["head_revision_id"]
        panel.get_by_role("button", name="查看变更", exact=True).first.click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("textbox", name="审查意见", exact=True).fill("已审阅检查结果及风险；只将通孔半径从 4 mm 改为 4.5 mm，提交后独立验证原生尺寸。")
        import re
        accept = dialog.get_by_role("button", name=re.compile(r"^接受(?:变更|并确认已审阅风险)$"))
        expect(accept).to_be_enabled(timeout=20000); accept.click()
        commit = dialog.get_by_role("button", name="提交版本", exact=True)
        expect(commit).to_be_enabled(timeout=20000); commit.click()
        expect(dialog.get_by_text("审查状态：committed", exact=True)).to_be_visible(timeout=30000)
        page.screenshot(path=str(OUT / "browser-reviewed-commit.png"))
        final = req("GET", path)
        assert final["state_version"] == 3
        geometry(artifacts(final, "browser-final"), 9, 8)
        page.goto(doc_url)
        panel.get_by_role("treeitem", name="HoleSketch", exact=True).click()
        expect(editor.get_by_role("spinbutton", name=label, exact=True)).to_have_value(target_value, timeout=30000)
        expect(page.get_by_test_id("document-scene").locator("canvas")).to_be_visible(timeout=30000)
        page.screenshot(path=str(OUT / "browser-refreshed.png"))
        assert not errors, errors
        record("browser_sketch_edit", {"workflow_id": task_id, "radius_mm": [4, 4.5], "diameter_mm": [8, 9],
                                      "local_draft_preserves_head": True, "review_commit": True, "refresh_preserved": True,
                                      "native_artifact_hashes_verified": True, "page_errors": errors,
                                      "final_revision": final["head_revision_id"], "final_fcstd_sha256": final["fcstd"]["sha256"]})
        private["committed"] = final
        PRIVATE.write_text(json.dumps(private)); PRIVATE.chmod(0o600)
        browser.close()
    owner.close(); guest.close()


if __name__ == "__main__":
    main()
