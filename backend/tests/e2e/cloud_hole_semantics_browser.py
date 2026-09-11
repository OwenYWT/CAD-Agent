"""Real browser -> provider -> durable workflow -> saved native/STEP hole cases.

Requires the private account created by cloud_document_acceptance.py. Run one
case at a time using CAD_HOLE_CASE=four_corners|blind_offset|different_diameters.
Then independently measure saved artifacts with freecad_hole_measurements.py.
"""
import hashlib
import json
import os
from pathlib import Path
import time

import httpx
from playwright.sync_api import expect, sync_playwright

from cloud_document_acceptance import BASE, PRIVATE, call

CASES = {
    "four_corners": {
        "prompt": "Create a rectangular mounting plate 80 mm long, 60 mm wide, 8 mm thick. It must have exactly FOUR separate 6 mm diameter THROUGH holes, one at each corner, each hole center 10 mm from its two nearest outer edges. Do not place any hole at the plate center. Keep square outside edges, no fillets or chamfers. Export STEP and STL.",
        "dimensions": [80, 60, 8],
        "holes": [{"x": x, "y": y, "diameter": 6, "depth": 8} for x in (10, 70) for y in (10, 50)],
    },
    "blind_offset": {
        "prompt": "Create an 80 mm long, 60 mm wide, 8 mm thick rectangular plate. Cut exactly one flat-bottom cylindrical BLIND pocket, diameter 6 mm and depth exactly 3 mm from the top face, center 20 mm from the left edge and 20 mm from the bottom edge. Leave exactly 5 mm of material beneath it. No center hole, no through hole, no fillets or chamfers. Export STEP and STL.",
        "dimensions": [80, 60, 8], "holes": [{"x": 20, "y": 20, "diameter": 6, "depth": 3}],
    },
    "different_diameters": {
        "prompt": "Create an 80 mm long, 60 mm wide, 8 mm thick rectangular plate with exactly TWO through holes. Measured from the lower left plate corner: first hole center at x=20 mm,y=20 mm, diameter 6 mm; second hole center at x=70 mm,y=50 mm, diameter 10 mm. No additional holes, no center hole, no fillets or chamfers. Export STEP and STL.",
        "dimensions": [80, 60, 8], "holes": [{"x": 20, "y": 20, "diameter": 6, "depth": 8}, {"x": 70, "y": 50, "diameter": 10, "depth": 8}],
    },
}


def run_case(name, case, out, *, expected_status="succeeded", on_result=None):
    """Submit real browser input and retain the actual task and artifact bytes."""
    out.mkdir(parents=True, exist_ok=True)
    private = json.loads(PRIVATE.read_text())
    client = httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]})
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        messages, errors = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def socket(ws):
            def received(raw):
                try:
                    messages.append(json.loads(raw))
                except (ValueError, TypeError):
                    pass
            ws.on("framereceived", received)
        page.on("websocket", socket)
        page.goto(os.getenv("CAD_NATIVE_E2E_WEB", BASE + "/"))
        page.locator('input[type="text"]').fill(private["owner"]["phone"])
        page.locator('input[type="password"]').fill(private["owner"]["password"])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role("button", name="新建设计", exact=True)).to_be_visible(timeout=20000)
        page.get_by_role("button", name="新建设计", exact=True).click()
        page.get_by_role("textbox", name="工程需求", exact=True).fill(case["prompt"])
        page.screenshot(path=str(out / "input.png"))
        page.get_by_role("button", name="开始创建", exact=False).click()
        deadline, submitted = time.monotonic() + 1800, None
        while time.monotonic() < deadline:
            submissions = [m["data"] for m in messages if m.get("type") == "task_submitted"]
            if submissions:
                submitted = submissions[-1]
                break
            page.wait_for_timeout(500)
        assert submitted, "browser did not submit a durable task"
        task_id, last = submitted["workflow_run_id"], None
        print(name, "workflow", task_id, flush=True)
        while time.monotonic() < deadline:
            task = call(client, "GET", f"/api/tasks/{task_id}/snapshot")
            stage = (task["status"], (task.get("agent") or {}).get("current_step_key"))
            if stage != last:
                print(name, stage, flush=True)
                last = stage
            if task["status"] == "waiting_confirmation":
                confirm = page.get_by_role("button", name="确认并继续", exact=True)
                if confirm.count():
                    confirm.click()
            if task["status"] in {"succeeded", "failed", "cancelled", "timed_out"}:
                break
            page.wait_for_timeout(2000)
        (out / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2))
        (out / "browser.json").write_text(json.dumps({"prompt": case["prompt"], **submitted, "status": task["status"], "page_errors": errors}, ensure_ascii=False, indent=2))
        page.screenshot(path=str(out / "result.png"))
        assert task["status"] == expected_status, {k: task.get(k) for k in ("status", "error_code", "error_message")}
        assert not errors, errors
        # Candidate artifacts are accessed by their authorized document endpoint;
        # creation success does not advance the branch head or accept the model.
        artifacts = {}
        for artifact in task["artifacts"]:
            kind = artifact["artifact_kind"]
            if kind not in {"fcstd", "step", "state"}:
                continue
            response = client.get(BASE + f"/api/documents/{submitted['branch_id']}/artifacts/{artifact['id']}")
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
            path = out / ("model." + ("json" if kind == "state" else kind))
            path.write_bytes(response.content)
            artifacts[kind] = str(path)
        if expected_status == "succeeded":
            assert {"fcstd", "step", "state"} <= set(artifacts)
        if "holes" in case:
            measurement = {"name": name, "dimensions": case["dimensions"], "holes": case["holes"], **artifacts}
            (out / "measurement-input.json").write_text(json.dumps([measurement], indent=2))
        if on_result is not None:
            on_result(page, task, submitted)
        browser.close()
        print("CAD_HOLE_BROWSER=" + json.dumps({"case": name, "workflow_id": task_id, "status": task["status"], "artifacts_downloaded": sorted(artifacts)}), flush=True)
        return task, submitted, artifacts


def main():
    name = os.environ["CAD_HOLE_CASE"]
    run_case(name, CASES[name], Path(os.environ["CAD_HOLE_REPORT_DIR"]) / name)


if __name__ == "__main__":
    main()
