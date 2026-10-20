"""Real HTTP/Chromium account switch, persisted balance and subscription.

Uses the isolated CI accounts and real database. No model requests or payments.
"""
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright


private = json.loads(Path(os.environ["CAD_NATIVE_E2E_PRIVATE"]).read_text())
out = Path(os.environ["CAD_BROWSER_CONTRACT_REPORT"]).parent / "browser-customer-billing"
out.mkdir(parents=True, exist_ok=False)
errors = []
with httpx.Client(base_url=os.environ["CAD_NATIVE_E2E_URL"]) as api:
    login = api.post("/api/auth/login/password", json={
        "phone": private["admin"]["phone"], "password": private["admin"]["password"],
    })
    login.raise_for_status()
    admin_headers = {"Authorization": "Bearer " + login.json()["token"]}
    owner_headers = {"Authorization": "Bearer " + private["owner"]["token"]}
    try:
        summary = api.get("/api/billing/me", headers=owner_headers)
        summary.raise_for_status()
        account = summary.json()
        assert account["wallet"]["balance_minor"] == 0
        tenant = account["tenant_id"]
        credit = api.post(f"/api/billing/admin/tenants/{tenant}/adjustments", headers=admin_headers, json={
            "amount_minor": 500, "reference": "browser-test", "reason": "测试人工余额调整",
            "idempotency_key": uuid4().hex,
        })
        credit.raise_for_status()
        assert credit.json()["kind"] == "admin_adjustment"
        catalog = api.get("/api/billing/admin/plans", headers=admin_headers)
        catalog.raise_for_status()
        version = max(row["version"] for row in catalog.json() if row["code"] == "pro") + 1
        plan = api.post("/api/billing/admin/plans", headers=admin_headers, json={
            "code": "pro", "version": version, "name": "专业版", "published": True,
            "monthly_price_minor": 100, "monthly_provider_requests": 3,
        })
        plan.raise_for_status()
        assigned = api.put(f"/api/billing/admin/tenants/{tenant}/subscription", headers=admin_headers, json={
            "plan_version_id": plan.json()["id"], "expected_revision": account["subscription"]["revision"],
            "idempotency_key": uuid4().hex,
        })
        assigned.raise_for_status()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                def sign_in(role):
                    page.goto(os.environ["CAD_NATIVE_E2E_WEB"])
                    page.locator("input[type=text]").fill(private[role]["phone"])
                    page.locator("input[type=password]").fill(private[role]["password"])
                    page.locator("button[type=submit]").click()
                    page.get_by_role("button", name="打开账号管理", exact=True).click()
                    return page.get_by_test_id("billing-account")

                panel = sign_in("owner")
                expect(panel).to_contain_text("专业版")
                expect(panel).to_contain_text("¥5.00")
                expect(panel).to_contain_text("0 / 3")
                expect(panel).to_contain_text("在线充值与自动扣费尚未开放")
                expect(page.get_by_label("实时任务连接正常", exact=True)).to_be_visible()
                token = page.evaluate('localStorage.getItem("cad_agent_auth_token")')
                # Token rotation must replace an idle modeling connection too;
                # otherwise the user's first new request hits a revoked socket.
                with page.expect_websocket(
                    predicate=lambda socket: "/ws/" in socket.url and "/ws/tasks/" not in socket.url,
                    timeout=5000,
                ):
                    page.get_by_role("button", name="刷新登录状态", exact=True).click()
                expect(page.get_by_text("登录状态已刷新", exact=True)).to_be_visible()
                expect(page.get_by_label("实时任务连接正常", exact=True)).to_be_visible()
                assert api.get("/api/billing/me", headers={"Authorization": "Bearer " + token}).status_code == 401
                token = page.evaluate('localStorage.getItem("cad_agent_auth_token")')
                assert api.get("/api/billing/me", headers={"Authorization": "Bearer " + token}).status_code == 200
                page.reload()
                page.get_by_role("button", name="打开账号管理", exact=True).click()
                expect(page.get_by_test_id("billing-account")).to_contain_text("¥5.00")
                page.get_by_role("button", name="退出登录", exact=True).click()
                expect(page.locator("input[type=password]")).to_be_visible()
                assert api.get("/api/billing/me", headers={"Authorization": "Bearer " + token}).status_code == 401
                panel = sign_in("editor")
                expect(panel).to_contain_text("基础版")
                expect(panel).to_contain_text("¥0.00")
                expect(panel).not_to_contain_text("¥5.00")
                expect(panel).not_to_contain_text("专业版")
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("button", name="打开项目导航", exact=True).click()
                page.get_by_role("button", name="打开账号管理", exact=True).click()
                expect(page.get_by_test_id("billing-account")).to_be_visible()
                expect(page.get_by_test_id("billing-account")).to_contain_text("¥0.00")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(out / "separate-account-mobile.png"))
                page.get_by_role("button", name="退出登录", exact=True).click()
                assert not errors, errors
            except BaseException:
                (out / "failure.txt").write_text(page.locator("body").inner_text())
                page.screenshot(path=str(out / "failure.png"))
                raise
            finally:
                browser.close()
        (out / "report.json").write_text(json.dumps({
            "passed": True, "real_auth": True, "real_postgres": True, "persisted_reload": True,
            "separate_accounts": True, "token_refresh_reconnects": True, "page_errors": errors,
        }, indent=2) + "\n")
        print("CUSTOMER BILLING BROWSER PASSED")
    finally:
        api.post("/api/auth/logout", headers=admin_headers).raise_for_status()
