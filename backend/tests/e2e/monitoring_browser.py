"""Independent monitoring UI against real auth, tasks and provider usage. No route mocks."""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

private = json.loads(Path(os.environ["CAD_MONITOR_E2E_PRIVATE"]).read_text())
report = Path(os.environ["CAD_MONITOR_E2E_REPORT"])
report.mkdir(parents=True, exist_ok=False)
report.chmod(0o700)
url = os.environ.get("CAD_MONITOR_E2E_URL", "http://127.0.0.1:8092/")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(url)
        for role in ("owner", "admin"):
            page.get_by_label("账号", exact=True).fill(private[role]["phone"])
            page.get_by_label("密码", exact=True).fill(private[role]["password"])
            page.get_by_role("button", name="登录监控面板").click()
            if role == "owner":
                expect(page.get_by_role("alert")).to_contain_text("仅平台管理员")
                expect(page.locator("#dashboard")).to_be_hidden()
        expect(page.get_by_role("heading", name="各账号使用情况")).to_be_visible()
        expect(page.get_by_role("button", name=private["owner"]["phone"], exact=True)).to_be_visible()
        page.get_by_role("button", name=private["owner"]["phone"], exact=True).click()
        expect(page.locator("#details-title")).to_contain_text(private["owner"]["phone"])
        expect(page.locator("#details")).to_contain_text(private["owner"]["phone"])
        page.get_by_role("button", name="模型调用", exact=True).click()
        expect(page.locator("#detail-head")).to_contain_text("总 Token")
        expect(page.locator("#details")).to_contain_text("kimi")
        expect(page.locator("#details")).not_to_contain_text(private["editor"]["phone"])
        page.locator("#details summary").first.click()
        expect(page.locator("#details details").first).to_contain_text('"workflow_run_id"')
        page.screenshot(path=str(report / "desktop.png"), full_page=True)
        page.reload()
        expect(page.get_by_role("heading", name="各账号使用情况")).to_be_visible()
        page.get_by_role("button", name=private["editor"]["phone"], exact=True).click()
        page.get_by_role("button", name="模型调用", exact=True).click()
        expect(page.locator("#details")).to_contain_text("kimi")
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(report / "mobile.png"), full_page=True)
        page.get_by_role("button", name="退出登录").click()
        expect(page.locator("#dashboard")).to_be_hidden()
        page.reload()
        expect(page.get_by_role("heading", name="管理员登录")).to_be_visible()
        assert not errors, errors
        (report / "report.json").write_text(json.dumps({"passed": True, "real_auth": True,
            "non_admin_denied": True, "two_accounts": True, "persistent_reload": True,
            "logout": True, "page_errors": errors}, indent=2))
        print("MONITORING BROWSER PASSED")
    except BaseException:
        page.screenshot(path=str(report / "failure.png"), full_page=True)
        raise
    finally:
        browser.close()
