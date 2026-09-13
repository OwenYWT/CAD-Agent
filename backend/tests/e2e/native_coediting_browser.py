"""Real-browser same-document editing, selection, review and immutable views.

Run after cloud_document_acceptance.py and cloud_document_browser.py using the
same isolated CAD_NATIVE_E2E_PRIVATE. No HTTP/provider/kernel substitutions.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

BASE = os.environ["CAD_NATIVE_E2E_URL"]
WEB = os.environ["CAD_NATIVE_E2E_WEB"]
PRIVATE = Path(os.environ["CAD_NATIVE_E2E_PRIVATE"])
REPORT = Path(os.environ["CAD_COEDIT_REPORT_DIR"])


def main():
    REPORT.mkdir(parents=True, exist_ok=True)
    person = json.loads(PRIVATE.read_text())
    client = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + person["owner"]["token"]}, timeout=60)
    document_url = "/api/documents/" + person["document_id"]
    evidence = {}

    def read(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def record(name, **facts):
        evidence[name] = facts
        (REPORT / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(name, json.dumps(facts, ensure_ascii=False), flush=True)

    def parameter(doc, feature_name, property_name):
        feature = next(f for f in doc["features"] if f["kernel_name"] == feature_name)
        return feature, next(p for p in feature["parameters"] if p["property_name"] == property_name)

    before = read(document_url)
    hole, diameter = parameter(before, "Hole", "Diameter")
    pad, length = parameter(before, "Pad", "Length")
    assert diameter["value"] == 9, "first run the real browser manual edit to 9 mm"
    target_length = length["value"] + 2
    old_operations = {op["id"] for op in read(document_url + "/collaboration")["operations"]}
    for invalid in ({"revision_id": str(uuid4())}, {"state_version": before["state_version"] + 1}, {"feature_ids": [str(uuid4())]}):
        selection = {"revision_id": before["head_revision_id"], "state_version": before["state_version"], "feature_ids": [pad["id"]], **invalid}
        response = client.post(document_url + "/operations", json={"action": "modify", "objective": "Modify the selected feature length",
            "expected_base_revision_id": before["head_revision_id"], "expected_state_version": before["state_version"],
            "idempotency_key": str(uuid4()), "selection_context": selection})
        assert response.status_code in {400, 409, 422}, response.text
    assert {op["id"] for op in read(document_url + "/collaboration")["operations"]} == old_operations
    record("invalid_selection", stale_revision=True, stale_generation=True, foreign_feature=True, kernel_jobs_created=0)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1440, "height": 960}, accept_downloads=True)
        errors, sent, received = [], [], []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def socket(ws):
            def capture(destination, payload):
                try:
                    item = json.loads(payload)
                    if item.get("type") in {"user_message", "task_submitted", "generation_result"}:
                        destination.append(item)
                except (ValueError, TypeError):
                    pass
            ws.on("framesent", lambda data: capture(sent, data))
            ws.on("framereceived", lambda data: capture(received, data))
        page.on("websocket", socket)
        try:
            page.goto(WEB)
            page.locator('input[type="text"]').fill(person["owner"]["phone"])
            page.locator('input[type="password"]').fill(person["owner"]["password"])
            page.locator('button[type="submit"]').click()
            page.get_by_role("button", name="Create a rectangular plate", exact=False).first.click()
            panel = page.get_by_test_id("cloud-document-panel")
            hole_item = panel.get_by_role("treeitem", name="Hole", exact=True)
            expect(hole_item).to_be_visible(timeout=20000)
            hole_item.click()
            field = panel.get_by_role("spinbutton", name=diameter["id"], exact=True)
            field.fill("10")
            panel.get_by_role("treeitem", name="Pad", exact=True).click()
            guard = page.get_by_role("dialog", name="保留编辑草稿")
            expect(guard).to_be_visible()
            guard.get_by_role("button", name="继续编辑", exact=True).click()
            expect(field).to_have_value("10")
            expect(hole_item).to_have_attribute("aria-selected", "true")
            panel.get_by_role("treeitem", name="Pad", exact=True).click()
            guard.get_by_role("button", name="放弃草稿并切换", exact=True).click()
            expect(panel.get_by_role("treeitem", name="Pad", exact=True)).to_have_attribute("aria-selected", "true")
            assert read(document_url)["state_version"] == before["state_version"]
            assert {op["id"] for op in read(document_url + "/collaboration")["operations"]} == old_operations
            expect(page.get_by_test_id("agent-selection").first).to_contain_text("Pad")
            page.screenshot(path=str(REPORT / "01-selected-pad.png"))
            record("draft_guard", retained_on_stay=True, explicit_discard=True, head_unchanged=True)

            prompt = (f"将当前选中的 Pad 特征的 Length 从 {length['value']:g} mm 改为 {target_length:g} mm。"
                      "继续编辑这个已提交的原生模型，保持 Hole 的孔径为 9 mm 和现有孔位置，不要重建其他特征。导出 FCStd、STEP 和 STL。")
            page.locator('textarea[aria-label="询问 Agent"]:visible').first.fill(prompt)
            page.get_by_role("button", name="审查请求", exact=True).click()
            page.get_by_role("button", name="确认并执行", exact=True).click()
            deadline = time.monotonic() + 90
            task_id = None
            while time.monotonic() < deadline:
                acknowledgements = [e["data"] for e in received if e["type"] == "task_submitted"]
                if acknowledgements:
                    task_id = acknowledgements[-1]["workflow_run_id"]
                    break
                failures = [e["data"] for e in received if e["type"] == "generation_result" and e["data"].get("error")]
                assert not failures, failures
                page.wait_for_timeout(250)
            assert task_id, "no durable acknowledgement from the real session socket"
            request = next(e for e in reversed(sent) if e["type"] == "user_message")
            assert request["operation_intent"] == "modify"
            assert request["selection_context"] == {"revision_id": before["head_revision_id"], "state_version": before["state_version"], "feature_ids": [pad["id"]]}
            record("ai_submission", workflow_id=task_id, selection=request["selection_context"], idempotency_key=request["idempotency_key"])
            deadline = time.monotonic() + 900
            last = None
            while time.monotonic() < deadline:
                task = read(f"/api/tasks/{task_id}/snapshot")
                if task["status"] != last:
                    last = task["status"]; print("AI task:", last, flush=True)
                if last == "waiting_confirmation":
                    button = page.get_by_role("button", name="确认并继续", exact=True)
                    expect(button).to_be_visible(timeout=20000); button.click()
                if last in {"succeeded", "failed", "cancelled", "timed_out"}:
                    break
                page.wait_for_timeout(2000)
            assert task["status"] == "succeeded", {k: task.get(k) for k in ("status", "error_code", "error_message", "error")}
            change_id = task["change_set"]["id"]
            detail = read("/api/change-sets/" + change_id)
            candidate = detail["candidate_revision_id"]
            assert read(document_url)["head_revision_id"] == before["head_revision_id"]
            page.get_by_role("button", name="预览候选变更", exact=True).click()
            expect(page.get_by_test_id("view-identity")).to_have_attribute("data-mode", "candidate")
            expect(page.get_by_test_id("view-identity")).to_have_attribute("data-revision", candidate)
            expect(page.get_by_test_id("document-scene")).to_have_attribute("data-revision", candidate, timeout=60000)
            return_to_head = page.get_by_role("button", name="查看已提交版本", exact=True)
            return_to_head.focus()
            return_to_head.press("Enter")
            expect(page.get_by_test_id("view-identity")).to_have_attribute("data-revision", before["head_revision_id"])
            panel.get_by_role("button", name="查看变更", exact=True).first.click()
            dialog = page.get_by_role("dialog", name="变更审查")
            expect(dialog).to_be_visible()
            dialog.get_by_role("textbox", name="审查意见", exact=True).fill("已核对候选：Pad Length 按指定值变化，保留已手改的 9 mm 孔径；查看实际门禁证据。")
            dialog.get_by_role("button", name="应用修改", exact=True).click()
            expect(dialog.get_by_text("审查状态：committed", exact=True)).to_be_visible(timeout=30000)
            dialog.get_by_role("button", name="关闭", exact=True).last.click()
            expect(page.get_by_test_id("view-identity")).to_have_attribute("data-revision", candidate, timeout=20000)
            after = read(document_url)
            assert after["head_revision_id"] == candidate and after["state_version"] == before["state_version"] + 1
            assert parameter(after, "Hole", "Diameter")[1]["value"] == 9
            assert parameter(after, "Pad", "Length")[1]["value"] == target_length
            assert {f["id"] for f in before["features"]} == {f["id"] for f in after["features"]}
            assert after["projector_version"] == 4 and after["hierarchy_status"] == "measured"
            body = next(f for f in after["features"] if f["kernel_name"] == "Body")
            assert body["structure"]["body_tip_id"] == hole["id"]
            assert parameter(after, "Pad", "Length")[0]["structure"]["container_ids"] == [body["id"]]
            record("same_model_ai_commit", source_revision=before["head_revision_id"], revision_id=candidate,
                change_set_id=change_id, state_version=after["state_version"], preserved_hole_mm=9, pad_length_mm=target_length,
                feature_ids_preserved=True, candidate_did_not_advance_head=True)
            page.reload()
            page.get_by_role("button", name="Create a rectangular plate", exact=False).first.click()
            expect(page.get_by_test_id("view-identity")).to_have_attribute("data-revision", candidate, timeout=20000)
            expect(page.get_by_role("treeitem", name="Pad", exact=True)).to_have_attribute("aria-level", "2")
            view = read(document_url + "/revisions/" + candidate)
            files = {}
            for kind in ("fcstd", "step", "stl"):
                response = client.get(view["snapshot"]["files"][kind]); response.raise_for_status()
                path = REPORT / ("committed." + ("FCStd" if kind == "fcstd" else kind))
                path.write_bytes(response.content)
                files[kind] = {"path": str(path), "sha256": hashlib.sha256(response.content).hexdigest(), "size_bytes": len(response.content)}
            (REPORT / "measurements.json").write_text(json.dumps([{"name": "manual-hole-then-selected-ai-pad", "fcstd": files["fcstd"]["path"],
                "step": files["step"]["path"], "dimensions": [60, 40, target_length], "holes": [{"x": 30, "y": 20, "diameter": 9, "depth": target_length}]}]))
            page.screenshot(path=str(REPORT / "02-ai-committed.png"))
            assert not errors, errors
            record("reload_and_files", revision_id=candidate, files=files, page_errors=errors)
        except BaseException:
            page.screenshot(path=str(REPORT / "failure.png"))
            (REPORT / "failure.txt").write_text(page.locator("body").inner_text())
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
