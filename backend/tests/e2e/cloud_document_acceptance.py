"""Live HTTP/WebSocket/Temporal/LLM/FreeCAD/S3 acceptance, no service doubles.

Run against an ISOLATED deployment with a bootstrap admin:
CAD_NATIVE_E2E_ENV=/private/path/test.env python tests/e2e/cloud_document_acceptance.py
The private session file supports subsequent browser verification; never commit it.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import dotenv_values
from websockets.sync.client import connect

BASE = os.getenv("CAD_NATIVE_E2E_URL", "http://127.0.0.1:8017")
PRIVATE = Path(os.getenv("CAD_NATIVE_E2E_PRIVATE", "/tmp/cad-native-p0-e2e-private.json"))
REPORT = Path(os.getenv("CAD_NATIVE_E2E_REPORT", "/tmp/cad-native-p0-e2e-report.json"))
evidence = {}


def write_review_evidence(path, review):
    """Retain review facts without publishing temporary object-store credentials."""
    public = copy.deepcopy(review)
    redacted = []
    for group in ('candidate_artifacts', 'artifacts'):
        for index, artifact in enumerate(public.get(group) or []):
            url = artifact.get('download_url')
            if not url:
                continue
            parsed = urlsplit(url)
            if parsed.query:
                artifact['download_url'] = urlunsplit(parsed._replace(query='', fragment=''))
                redacted.append(f'{group}[{index}].download_url query')
    if redacted:
        public['_evidence_redactions'] = redacted
    Path(path).write_text(json.dumps(public, ensure_ascii=False, indent=2))


def call(client, method, path, *, expected=200, **kwargs):
    response = client.request(method, BASE + path, timeout=60, **kwargs)
    assert response.status_code == expected, (method, path, response.status_code, response.text[:1600])
    return response.json() if response.content else None


def wait_task(client, task_id, timeout=900):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        task = call(client, "GET", f"/api/tasks/{task_id}/snapshot")
        status = task["status"]
        if status != last:
            print(f"task {task_id[:8]}: {status}", flush=True)
            last = status
        if status == "waiting_confirmation":
            call(client, "POST", f"/api/tasks/{task_id}/confirmation", json={"accepted": True, "note": "Live integration test plan reviewed"})
        if status in {"succeeded", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(2)
    raise TimeoutError(f"task {task_id} did not complete")


def commit(client, task):
    assert task["status"] == "succeeded", {k: task.get(k) for k in ("status", "error_code", "error_message", "error")}
    change = task["change_set"]["id"]
    call(client, "POST", f"/api/change-sets/{change}/accept", json={"note": "Real kernel and gate evidence reviewed by acceptance test"})
    call(client, "POST", f"/api/change-sets/{change}/commit")
    return change


def accept_in_browser(dialog, note):
    """Use the actual review controls, including explicit advisory-risk consent.

    A failed required/design gate keeps this button disabled and fails the test.
    The caller independently verifies the resulting native parameters/artifacts.
    """
    import re
    from playwright.sync_api import expect

    dialog.get_by_role("textbox", name="审查意见", exact=True).fill(note)
    accept = dialog.get_by_role("button", name=re.compile(r"^接受(?:变更|并确认已审阅风险)$"))
    expect(accept).to_be_enabled(timeout=15000)
    accept.click()


def main():
    config = dotenv_values(os.environ["CAD_NATIVE_E2E_ENV"])
    assert config.get("ADMIN_PASSWORD"), "isolated bootstrap admin password required"
    admin = httpx.Client()
    call(admin, "GET", "/ready")
    session = call(admin, "POST", "/api/auth/login/password", json={"phone": "admin", "password": config["ADMIN_PASSWORD"]})
    admin.headers["Authorization"] = "Bearer " + session["token"]
    people = []
    for index in range(2):
        invite = call(admin, "POST", "/api/auth/invites", json={"max_uses": 1})
        phone = "189" + str((time.time_ns() + index) % 100000000).zfill(8)
        password = secrets.token_urlsafe(24)
        person = call(admin, "POST", "/api/auth/register/invite", json={"phone": phone, "password": password, "invite_code": invite["code"]})
        people.append({**person, "password": password, "phone": phone})
    PRIVATE.touch(mode=0o600); PRIVATE.chmod(0o600)
    private = {"owner": people[0], "guest": people[1]}
    PRIVATE.write_text(json.dumps(private))
    owner = httpx.Client(headers={"Authorization": "Bearer " + people[0]["token"]})
    guest = httpx.Client(headers={"Authorization": "Bearer " + people[1]["token"]})
    ws_session, panel = str(uuid4()), str(uuid4())
    protocol = "cad-agent-auth." + base64.urlsafe_b64encode(people[0]["token"].encode()).decode().rstrip("=")
    with connect(BASE.replace("http", "ws", 1) + f"/ws/{ws_session}", subprotocols=[protocol]) as socket:
        socket.send(json.dumps({"type": "user_message", "panel_id": panel, "operation_intent": "generate",
            "text": "Create a rectangular plate 60 mm by 40 mm, thickness 8 mm. Add one centered 6 mm diameter through hole. Export STEP and STL.", "idempotency_key": str(uuid4())}))
        for _ in range(30):
            event = json.loads(socket.recv(timeout=60))
            if event["type"] == "task_submitted":
                submitted = event["data"]
                break
            if event["type"] == "generation_result" and event["data"].get("error"):
                raise AssertionError(event["data"]["error"])
        else:
            raise AssertionError("session socket did not submit durable work")
    doc_id = submitted["branch_id"]
    private.update(document_id=doc_id, project_id=submitted["project_id"], session_id=ws_session, panel_id=panel,
                   generation_workflow_id=submitted["workflow_run_id"])
    PRIVATE.write_text(json.dumps(private))
    print("Created real document and generation task", flush=True)
    initial = call(owner, "GET", f"/api/documents/{doc_id}")
    assert initial["state_version"] == 0 and initial["mesh"] is None
    task = wait_task(owner, submitted["workflow_run_id"])
    uncommitted = call(owner, "GET", f"/api/documents/{doc_id}")
    assert uncommitted["head_revision_id"] == initial["head_revision_id"]
    first_change = commit(owner, task)
    first = call(owner, "GET", f"/api/documents/{doc_id}")
    assert first["state_version"] == 1 and first["fcstd"] and first["mesh"] and first["features"]
    for kind in ("fcstd", "mesh", "state"):
        artifact = first[kind]
        response = owner.get(BASE + artifact["url"])
        assert response.status_code == 200 and hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
    hole = next(f for f in first["features"] if f["type"] == "PartDesign::Hole")
    diameter = next(p for p in hole["parameters"] if p["property_name"] == "Diameter")
    assert diameter["value"] == 6
    assert first["projector_version"] == 4
    assert all(f["revision_created"] == first["head_revision_id"] for f in first["features"])
    assert any(f["topology_bindings"] for f in first["features"])
    assert all(b["revision_id"] == first["head_revision_id"] for f in first["features"] for b in f["topology_bindings"])
    evidence["generation"] = {"workflow_id": task["id"], "revision_id": first["head_revision_id"], "feature_count": len(first["features"]), "change_set_id": first_change}
    modification = {"action": "parameters.update", "expected_base_revision_id": first["head_revision_id"], "expected_state_version": first["state_version"],
        "idempotency_key": str(uuid4()), "modification": {"expected_state_sha256": first["parameter_state_sha256"], "parameter_updates": [{"parameter_id": diameter["id"], "value": 8}]}}
    modified_task = call(owner, "POST", f"/api/documents/{doc_id}/operations", expected=202, json=modification)
    replay = call(owner, "POST", f"/api/documents/{doc_id}/operations", expected=202, json=modification)
    assert replay["workflow_run_id"] == modified_task["workflow_run_id"]
    changed = wait_task(owner, modified_task["workflow_run_id"])
    second_change = commit(owner, changed)
    second = call(owner, "GET", f"/api/documents/{doc_id}")
    updated_hole = next(f for f in second["features"] if f["id"] == hole["id"])
    assert next(p for p in updated_hole["parameters"] if p["id"] == diameter["id"])["value"] == 8
    assert updated_hole["revision_created"] == first["head_revision_id"]
    assert updated_hole["last_modified"] == second["head_revision_id"]
    assert next(f for f in second["features"] if f["kernel_name"] == "Pad")["last_modified"] == first["head_revision_id"]
    assert all(b["revision_id"] == second["head_revision_id"] for f in second["features"] for b in f["topology_bindings"])
    assert {f["id"] for f in first["features"]} == {f["id"] for f in second["features"]}
    assert first["fcstd"]["sha256"] != second["fcstd"]["sha256"] and first["mesh"]["sha256"] != second["mesh"]["sha256"]
    events = call(owner, "GET", f"/api/documents/{doc_id}/events?after={first['event_sequence']}")["events"]
    delta = next(e for e in events if e["event_type"] == "state_delta")
    assert delta["payload"]["state_version"] == 2 and delta["payload"]["removed"] == []
    stale = {**modification, "idempotency_key": str(uuid4())}
    call(owner, "POST", f"/api/documents/{doc_id}/operations", expected=409, json=stale)
    replay = call(owner, "POST", f"/api/documents/{doc_id}/operations", expected=202, json=modification)
    assert replay["workflow_run_id"] == modified_task["workflow_run_id"]
    evidence["local_parameter_edit"] = {"workflow_id": modified_task["workflow_run_id"], "state_version": second["state_version"], "diameter_before_mm": 6, "diameter_after_mm": 8,
        "delta_feature_count": len(delta["payload"]["upserted"]), "stable_feature_ids": True, "stale_rejected": True, "idempotency_verified": True,
        "feature_history_and_current_topology_revisions_verified": True}
    invitation = call(owner, "POST", f"/api/documents/{doc_id}/invitations", expected=201)
    call(guest, "GET", f"/api/documents/{doc_id}", expected=404)
    call(guest, "POST", f"/api/documents/{doc_id}/invitations/accept", json={"tenant_id": invitation["tenant_id"], "token": invitation["token"]})
    guest.headers["X-Workspace-Tenant"] = invitation["tenant_id"]
    shared = call(guest, "GET", f"/api/documents/{doc_id}")
    assert not shared["can_edit"]
    call(guest, "POST", f"/api/documents/{doc_id}/operations", expected=403, json=stale)
    comment_id = str(uuid4())
    call(guest, "POST", f"/api/documents/{doc_id}/comments", expected=201, json={"comment_id": comment_id, "revision_id": second["head_revision_id"], "feature_id": hole["id"], "body": "实测审阅：中心孔已从 6 mm 调整为 8 mm。"})
    call(guest, "POST", f"/api/documents/{doc_id}/presence", expected=204, json={"client_id": str(uuid4()), "selected_feature_id": hole["id"]})
    collaboration = call(owner, "GET", f"/api/documents/{doc_id}/collaboration")
    assert any(c["id"] == comment_id for c in collaboration["comments"]) and collaboration["presence"]
    evidence["collaboration"] = {"cross_account_view": True, "viewer_write_denied": True, "comment_visible_to_owner": True}
    private.update(tenant_id=invitation["tenant_id"], last_change_set_id=second_change, committed=second,
                   last_workflow_id=modified_task["workflow_run_id"])
    PRIVATE.write_text(json.dumps(private))
    REPORT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
