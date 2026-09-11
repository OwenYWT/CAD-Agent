"""Real browser acceptance against the isolated live-service acceptance project.

Requires Python Playwright and its Chromium browser. No route interception or
service substitutes. Credentials are read from the private live acceptance file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import httpx
from playwright.sync_api import expect, sync_playwright
from cloud_document_acceptance import accept_in_browser

PRIVATE = Path(os.getenv("CAD_NATIVE_E2E_PRIVATE", "/tmp/cad-native-p0-e2e-private.json"))
BASE = os.getenv("CAD_NATIVE_E2E_URL", "http://127.0.0.1:8017")
WEB = os.getenv("CAD_NATIVE_E2E_WEB", "http://127.0.0.1:5179")
REPORT = Path(os.getenv("CAD_NATIVE_BROWSER_REPORT", "/tmp/cad-native-p0-browser-report.json"))
SHOTS = Path(os.getenv("CAD_NATIVE_BROWSER_SHOTS", "/tmp/cad-native-p0-browser"))


def main():
    private = json.loads(PRIVATE.read_text())
    SHOTS.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + private["owner"]["token"]}, timeout=30)
    doc_url = f"/api/documents/{private['document_id']}"
    evidence = {}

    def read(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def record(stage, **facts):
        evidence[stage] = facts
        REPORT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(stage, json.dumps(facts, ensure_ascii=False), flush=True)

    def login(page, person):
        page.goto(WEB)
        page.locator('input[type="text"]').fill(person["phone"])
        page.locator('input[type="password"]').fill(person["password"])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role("button", name="新建设计", exact=True)).to_be_visible(timeout=20000)

    def restore(page):
        page.get_by_role("button", name="Create a rectangular plate", exact=False).first.click()
        panel = page.get_by_test_id("cloud-document-panel")
        expect(panel.get_by_role("treeitem", name="Hole", exact=True)).to_be_visible(timeout=20000)
        panel.get_by_role("treeitem", name="Hole", exact=True).click()
        return panel

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        owner_context = browser.new_context(viewport={"width": 1440, "height": 900})
        owner = owner_context.new_page()
        errors = []
        owner.on("pageerror", lambda error: errors.append(str(error)))
        login(owner, private["owner"])
        panel = restore(owner)
        before = read(doc_url)
        hole = next(f for f in before["features"] if f["type"] == "PartDesign::Hole")
        diameter = next(p for p in hole["parameters"] if p["property_name"] == "Diameter")
        target = float(diameter["value"]) + 1
        expect(panel.get_by_role("spinbutton", name=diameter["id"], exact=True)).to_have_value(str(int(diameter["value"])))
        expect(owner.locator("canvas")).to_be_visible(timeout=20000)
        assert owner.locator("canvas").evaluate("c => !!c.getContext('webgl2')")
        owner.screenshot(path=str(SHOTS / "01-restored.png"))
        record("restore", state_version=before["state_version"], feature_id=hole["id"], diameter=diameter["value"], real_webgl=True)

        operations_before = len(read(doc_url + "/collaboration")["operations"])
        rect = owner.locator("canvas").bounding_box()
        owner.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
        owner.mouse.down()
        owner.mouse.move(rect["x"] + rect["width"] / 2 + 70, rect["y"] + rect["height"] / 2 + 30, steps=8)
        owner.mouse.up()
        assert len(read(doc_url + "/collaboration")["operations"]) == operations_before
        record("local_camera", kernel_operations_created=0)

        intent = f"浏览器实测：保持同心，当前目标孔径 {target} mm"
        panel.get_by_role("textbox",name="特征用途",exact=True).fill("定位孔")
        panel.get_by_role("textbox",name="特征设计意图",exact=True).fill(intent)
        panel.get_by_role("button",name="保存特征标注",exact=True).click()
        for _ in range(40):
            annotated=read(doc_url)
            annotated_hole=next(f for f in annotated['features'] if f['id']==hole['id'])
            if annotated_hole['intent']==intent: break
            owner.wait_for_timeout(250)
        else: raise AssertionError('feature annotation was not persisted')
        assert annotated['state_version']==before['state_version']
        panel.get_by_role("button",name="内核属性",exact=True).click()
        expect(panel.get_by_text("共 ",exact=False).last).to_be_visible(timeout=10000)
        assert len(read(doc_url + '/collaboration')['operations'])==operations_before
        record('semantic_context',annotation_version=annotated_hole['annotation_version'],intent_persisted=True,inspection_created_kernel_jobs=0)

        panel.get_by_role("spinbutton", name=diameter["id"], exact=True).fill(str(target))
        with owner.expect_response(lambda r: r.url.endswith(doc_url + "/operations") and r.request.method == "POST") as response:
            panel.get_by_role("button", name="提交参数变更", exact=True).click()
        assert response.value.status == 202, response.value.text()
        task_id = response.value.json()["workflow_run_id"]
        record("parameter_submitted", workflow_id=task_id, diameter_target=target)
        deadline = time.monotonic() + 600
        last = None
        while time.monotonic() < deadline:
            task = read(f"/api/tasks/{task_id}/snapshot")
            if task["status"] != last:
                print("browser task:", task["status"], flush=True)
                last = task["status"]
            if last == "waiting_confirmation":
                button = owner.get_by_role("button", name="确认并继续", exact=True)
                expect(button).to_be_visible(timeout=15000)
                button.click()
            elif last in {"succeeded", "failed", "cancelled", "timed_out"}:
                break
            owner.wait_for_timeout(2000)
        assert task["status"] == "succeeded", {k: task.get(k) for k in ("status", "error_code", "error_message")}
        assert read(doc_url)["head_revision_id"] == before["head_revision_id"]
        expect(panel.get_by_text(f"v{before['state_version']}", exact=True)).to_be_visible()
        panel.get_by_role("button", name="查看变更", exact=True).first.click()
        dialog = owner.get_by_role("dialog")
        accept_in_browser(dialog, "已阅读检查结果与测量限制；本次仅修改孔径，并核对提交后的原生参数和导出文件。")
        expect(dialog.get_by_role("button", name="提交版本", exact=True)).to_be_enabled(timeout=15000)
        dialog.get_by_role("button", name="提交版本", exact=True).click()
        expect(dialog.get_by_text("审查状态：committed", exact=True)).to_be_visible(timeout=20000)
        owner.screenshot(path=str(SHOTS / "02-reviewed-commit.png"))
        dialog.get_by_role("button", name="关闭", exact=True).last.click()
        expect(panel.get_by_text(f"v{before['state_version'] + 1}", exact=True)).to_be_visible(timeout=15000)
        after = read(doc_url)
        updated = next(f for f in after["features"] if f["id"] == hole["id"])
        assert next(p for p in updated["parameters"] if p["id"] == diameter["id"])["value"] == target
        assert after["mesh"]["sha256"] != before["mesh"]["sha256"]
        record("ui_review_commit", state_version=after["state_version"], stable_feature_id=True, changed_mesh=True)

        owner.reload()
        panel = restore(owner)
        expect(panel.get_by_role("spinbutton", name=diameter["id"], exact=True)).to_have_value(str(int(target)))
        expect(owner.locator("canvas")).to_be_visible(timeout=20000)
        record("reload_after_commit", same_document=True, parameter_value=target)
        owner_note = f"浏览器实测：孔径已提交为 {target:g} mm。"
        panel.get_by_role("textbox", name="文档评论", exact=True).fill(owner_note)
        panel.get_by_role("button", name="保存评论", exact=True).click()
        expect(panel.get_by_text(owner_note, exact=True)).to_be_visible(timeout=15000)
        panel.get_by_role("button", name="邀请项目审阅者", exact=True).click()
        invitation = panel.get_by_role("textbox", name="项目审阅邀请链接", exact=True)
        expect(invitation).to_be_visible(timeout=15000)
        link = invitation.input_value()

        guest_context = browser.new_context(viewport={"width": 1280, "height": 900})
        guest = guest_context.new_page()
        guest.on("pageerror", lambda error: errors.append(str(error)))
        login(guest, private["guest"])
        guest.goto(link)
        guest_panel = guest.get_by_test_id("cloud-document-panel")
        expect(guest_panel.get_by_role("treeitem", name="Hole", exact=True)).to_be_visible(timeout=20000)
        guest_panel.get_by_role("treeitem", name="Hole", exact=True).click()
        assert guest_panel.get_by_role("spinbutton").count() == 0
        assert guest_panel.get_by_role("button", name="提交参数变更", exact=True).count() == 0
        assert "invite=" not in guest.url
        expect(guest.locator("canvas")).to_be_visible(timeout=20000)
        expect(guest_panel.get_by_text(owner_note, exact=True)).to_be_visible(timeout=15000)
        guest_note = f"受邀账号浏览器复核：{target:g} mm 孔径，特征评论可见。"
        guest_panel.get_by_role("textbox", name="文档评论", exact=True).fill(guest_note)
        guest_panel.get_by_role("button", name="保存评论", exact=True).click()
        expect(panel.get_by_text(guest_note, exact=True)).to_be_visible(timeout=20000)
        owner.wait_for_timeout(16000)
        assert len(read(doc_url + "/collaboration")["presence"]) >= 2
        guest.screenshot(path=str(SHOTS / "03-shared-review.png"))
        record("cross_account_browser", readonly=True, both_comments_visible=True, presence_sessions_at_least=2)
        guest.set_viewport_size({"width": 390, "height": 844})
        guest_panel.get_by_role("textbox", name="文档评论", exact=True).scroll_into_view_if_needed()
        guest.screenshot(path=str(SHOTS / "04-mobile-review.png"))
        assert guest.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        record("browser_complete", page_errors=errors, mobile_no_horizontal_overflow=True, screenshots=str(SHOTS))
        browser.close()
    client.close()


if __name__ == "__main__":
    main()
