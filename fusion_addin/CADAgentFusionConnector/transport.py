"""Standard-library HTTP transport worker; contains no Autodesk API imports."""

from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import ConnectorConfig
from .dispatcher import CancelToken
from .protocol import lease_identity, validate_task

log = logging.getLogger("CADAgentFusionConnector.transport")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Fusion Runtime redirects are not allowed")


class RuntimeHttpClient:
    def __init__(self, config: ConnectorConfig):
        self.config = config
        self._headers = {
            "Authorization": f"Bearer {config.connector_secret}",
            "Content-Type": "application/json",
            "User-Agent": "CAD-Agent-Fusion-Connector/1.0.0",
        }
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | None]:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.config.runtime_url + path,
            data=data,
            method=method,
            headers=self._headers,
        )
        try:
            with self._opener.open(request, timeout=self.config.request_timeout_s) as response:
                if response.status == 204:
                    return 204, None
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise RuntimeError("Runtime response exceeds 1 MiB")
                return response.status, json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read(64 * 1024)
            try:
                detail = json.loads(raw.decode("utf-8"))
            except Exception:
                detail = {"status": exc.code}
            raise RuntimeError(f"Runtime HTTP {exc.code}: {detail.get('error', {}).get('code', 'unknown')}") from exc

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        _, body = self.request("POST", "/v1/connector/register", payload)
        return body or {}

    def heartbeat(self, connector_id: str) -> None:
        self.request("POST", "/v1/connector/heartbeat", {"connector_instance_id": connector_id})

    def poll(self, connector_id: str) -> dict[str, Any] | None:
        status, body = self.request("GET", f"/v1/connector/{connector_id}/tasks/next")
        return None if status == 204 else validate_task(body or {})

    def started(self, task: dict[str, Any], connector_id: str) -> None:
        self.request(
            "POST",
            f"/v1/connector/tasks/{task['request_id']}/started",
            lease_identity(task, connector_id),
        )

    def result(self, task: dict[str, Any], connector_id: str, result: dict[str, Any]) -> None:
        local_artifacts = result.get("_local_artifacts", [])
        snapshot_evidence = result.get("_snapshot")
        public_result = {
            key: value for key, value in result.items()
            if key not in {"_local_artifacts", "_snapshot"}
        }
        self.request(
            "POST",
            f"/v1/connector/tasks/{task['request_id']}/result",
            {
                **lease_identity(task, connector_id),
                "result": public_result,
                "local_artifacts": local_artifacts,
                "snapshot_evidence": snapshot_evidence,
            },
        )

    def control(self, task: dict[str, Any], connector_id: str) -> dict[str, Any]:
        path = (
            f"/v1/connector/{connector_id}/tasks/{task['request_id']}/control"
            f"?lease_id={task['lease_id']}&attempt={task['attempt']}"
        )
        _, body = self.request("GET", path)
        return body or {"cancel_requested": False, "lease_valid": False}


class ConnectorWorker:
    """Network worker and thread-safe bridge to Fusion's CustomEvent handler."""

    def __init__(
        self,
        config: ConnectorConfig,
        registration: dict[str, Any],
        inbound: queue.Queue,
        outbound: queue.Queue,
        signal_main_thread: Callable[[], None],
        client: RuntimeHttpClient | None = None,
    ):
        self.config = config
        self.registration = registration
        self.inbound = inbound
        self.outbound = outbound
        self.signal_main_thread = signal_main_thread
        self.client = client or RuntimeHttpClient(config)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.cancel_tokens: dict[str, CancelToken] = {}
        self.current_task: dict[str, Any] | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="CADAgentFusionTransport", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=timeout)

    def _run(self) -> None:
        backoff = 1.0
        registered = False
        last_heartbeat = 0.0
        while not self.stop_event.is_set():
            try:
                if not registered:
                    self.client.register(self.registration)
                    registered = True
                now = time.monotonic()
                if now - last_heartbeat >= 5.0:
                    self.client.heartbeat(self.config.connector_instance_id)
                    last_heartbeat = now
                self._flush_results()
                if self.current_task:
                    self._update_control()
                else:
                    task = self.client.poll(self.config.connector_instance_id)
                    if task:
                        token = CancelToken()
                        self.current_task = task
                        self.cancel_tokens[str(task["request_id"])] = token
                        self.client.started(task, self.config.connector_instance_id)
                        self.inbound.put((task, token))
                        # Official CustomEvent is the sole worker-to-main-thread signal.
                        self.signal_main_thread()
                backoff = 1.0
                self.stop_event.wait(self.config.poll_interval_s)
            except Exception as exc:
                registered = False
                log.warning("Fusion Runtime connection unavailable; retrying", extra={"delay_s": backoff})
                self.stop_event.wait(backoff + random.random() * min(1.0, backoff / 4))
                backoff = min(30.0, backoff * 2)

    def _flush_results(self) -> None:
        try:
            while True:
                task, result = self.outbound.get_nowait()
                try:
                    self.client.result(task, self.config.connector_instance_id, result)
                except Exception:
                    # Preserve the exact durable result for retry. Runtime accepts a
                    # duplicate result for the same fenced lease idempotently.
                    self.outbound.put((task, result))
                    raise
                self.cancel_tokens.pop(str(task["request_id"]), None)
                if self.current_task and self.current_task["request_id"] == task["request_id"]:
                    self.current_task = None
        except queue.Empty:
            return

    def _update_control(self) -> None:
        task = self.current_task
        if not task:
            return
        control = self.client.control(task, self.config.connector_instance_id)
        token = self.cancel_tokens.get(str(task["request_id"]))
        if token:
            token.cancel_requested = bool(control.get("cancel_requested"))
            token.lease_valid = bool(control.get("lease_valid"))
