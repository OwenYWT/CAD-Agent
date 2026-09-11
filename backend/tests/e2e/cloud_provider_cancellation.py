"""Cancel a real provider operation through the public durable task API."""
import base64
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx
from websockets.sync.client import connect
from cloud_document_acceptance import BASE, PRIVATE, call


def main():
    private = json.loads(PRIVATE.read_text())
    client = httpx.Client(headers={"Authorization": "Bearer " + private["owner"]["token"]})
    out = Path(os.environ["CAD_CANCELLATION_REPORT_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    protocol = "cad-agent-auth." + base64.urlsafe_b64encode(private["owner"]["token"].encode()).decode().rstrip("=")
    with connect(BASE.replace("http", "ws", 1) + f"/ws/{uuid4()}", subprotocols=[protocol]) as socket:
        socket.send(json.dumps({"type": "user_message", "panel_id": str(uuid4()), "operation_intent": "generate",
            "idempotency_key": str(uuid4()), "text": (
                "Create a 120x80x8 mm rectangular plate with exactly eight 6 mm through holes. "
                "Hole centers relative to the lower left corner: (10,10),(40,10),(80,10),(110,10),"
                "(10,70),(40,70),(80,70),(110,70). Keep square edges and export STEP and STL."
            )}))
        while True:
            event = json.loads(socket.recv(timeout=60))
            if event["type"] == "task_submitted":
                submitted = event["data"]
                break
    (out / "submission.json").write_text(json.dumps(submitted, indent=2))
    task_id = submitted["workflow_run_id"]
    print("Cancellation test submitted", task_id, flush=True)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        task = call(client, "GET", f"/api/tasks/{task_id}/snapshot")
        if task["status"] == "waiting_confirmation":
            call(client, "POST", f"/api/tasks/{task_id}/confirmation", json={"accepted": True})
        if task["status"] == "running" and task["agent"]["current_step_kind"] == "agent_freecad_operations":
            break
        assert task["status"] not in {"failed", "succeeded", "cancelled", "timed_out"}, task["status"]
        time.sleep(0.5)
    else:
        raise AssertionError("provider operation was never reached")
    start = time.monotonic()
    call(client, "POST", f"/api/tasks/{task_id}/cancel", json={"reason": "Real cancellation during provider generation acceptance"})
    while time.monotonic() - start < 20:
        task = call(client, "GET", f"/api/tasks/{task_id}/snapshot")
        if task["status"] == "cancelled":
            break
        time.sleep(0.5)
    assert task["status"] == "cancelled", task["status"]
    elapsed = time.monotonic() - start
    time.sleep(20)
    task = call(client, "GET", f"/api/tasks/{task_id}/snapshot")
    document = call(client, "GET", "/api/documents/" + submitted["branch_id"])
    assert task["status"] == "cancelled" and not task["artifacts"] and not task["change_set"]
    assert document["state_version"] == 0 and not document["fcstd"]
    (out / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2))
    evidence = {"workflow_id": task_id, "cancelled_within_seconds": round(elapsed, 3),
        "no_late_artifacts_or_candidate": True, "head_unchanged": True}
    (out / "summary.json").write_text(json.dumps(evidence, indent=2))
    print("CAD_PROVIDER_CANCELLATION=" + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
