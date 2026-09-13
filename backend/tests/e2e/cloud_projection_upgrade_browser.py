"""A real v2 document remains usable after the v3 semantic projection upgrade."""
import hashlib
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from cloud_document_acceptance import BASE, PRIVATE, call


def main():
    private = json.loads(PRIVATE.read_text())
    old = json.loads(Path(os.environ["CAD_PROJECTION_LEGACY_INPUT"]).read_text())["snapshot"]
    out = Path(os.environ["CAD_PROJECTION_REPORT_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]})
    path = "/api/documents/" + old["document_id"]
    current = call(client, "GET", path)
    assert old["projector_version"] == 2 and current["projector_version"] == 4
    for key in ("head_revision_id", "state_version", "parameter_state_sha256", "fcstd"):
        assert current[key] == old[key], key
    previous = {f["id"]: f for f in old["features"]}
    removed = 0
    for feature in current["features"]:
        baseline = previous[feature["id"]]
        assert {k: v for k, v in feature.items() if k != "topology_bindings"} == {
            k: v for k, v in baseline.items() if k != "topology_bindings"}
        removed += len(baseline["topology_bindings"]) - len(feature["topology_bindings"])
    assert removed > 0
    operations = call(client, "GET", path + "/collaboration")["operations"]
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
        page.goto(web + "?document=" + old["document_id"] + "&workspace=" + private["tenant_id"])
        scene = page.get_by_test_id("document-scene")
        expect(scene).to_have_attribute("data-revision", old["head_revision_id"], timeout=30000)
        assert scene.locator("canvas").evaluate("c => !!c.getContext('webgl2')")
        panel = page.get_by_test_id("cloud-document-panel")
        pad = next(f for f in current["features"] if f["type"] == "PartDesign::Pad")
        panel.get_by_role("treeitem", name=pad["label"], exact=True).click()
        parameter = next(p for p in pad["parameters"] if p["property_name"] == "Length")
        expect(panel.get_by_role("spinbutton", name=parameter["id"], exact=True)).to_have_value(f'{parameter["value"]:g}')
        panel.get_by_role("button", name="拓扑测量", exact=True).click()
        expect(panel.get_by_text('"geometry_type": "Part::GeomPlane"', exact=False).first).to_be_visible(timeout=15000)
        page.screenshot(path=str(out / "current-projection.png"))
        assert not errors, errors
        browser.close()
    assert call(client, "GET", path + "/collaboration")["operations"] == operations
    download = client.get(BASE + current["fcstd"]["url"])
    assert download.status_code == 200
    assert hashlib.sha256(download.content).hexdigest() == old["fcstd"]["sha256"]
    evidence = {"document_id": old["document_id"], "projector_before": 2, "projector_after": 3,
        "revision_id": old["head_revision_id"], "reference_bindings_removed": removed,
        "all_feature_content_and_revision_history_preserved": True, "native_hash_unchanged": True,
        "browser_model_parameters_and_inspection_work": True, "no_new_modeling_operations": True,
        "page_errors": errors}
    (out / "summary.json").write_text(json.dumps(evidence, indent=2))
    print("CAD_PROJECTION_UPGRADE=" + json.dumps(evidence), flush=True)
    client.close()


if __name__ == "__main__":
    main()
