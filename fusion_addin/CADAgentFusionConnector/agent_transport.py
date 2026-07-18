"""Dependency-free HTTPS transport for the direct Cloud Agent mode.

This module deliberately imports no Autodesk modules.  Worker messages cross
the thread boundary only after a strict JSON round-trip; the sole callback from
the worker is the CustomEvent signal supplied by the lifecycle module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import ConnectorConfig


log = logging.getLogger("CADAgentFusionConnector.agent_transport")
PLAN_PATH = "/api/cad/fusion360/agent/plan"
REPORT_PATH = "/api/cad/fusion360/agent/results"
ARTIFACT_PATH = "/api/cad/fusion360/agent/artifacts/{request_id}/{filename}"
HEARTBEAT_PATH = "/api/cad/fusion360/agent/heartbeat"
CAPABILITIES_PATH = "/api/cad/fusion360/agent/capabilities"
_RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
_UPLOAD_FIELDS = {
    "request_id", "artifact_id", "filename", "format", "size_bytes", "sha256",
    "upload_authorized", "f3d_upload_authorized",
}


class AgentTransportError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False,
        retry_after_s: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_s = retry_after_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "category": "transport",
            "retryable": self.retryable,
            "details": {},
        }


def load_bearer_token(path: Path | None = None) -> str:
    """Load a bearer token without placing it in ConnectorConfig or logs."""

    if path is None:
        token = os.environ.get("CAD_AGENT_FUSION_TOKEN", "")
    else:
        path = Path(path).expanduser()
        try:
            info = path.stat()
            if os.name != "nt" and info.st_mode & 0o077:
                raise AgentTransportError(
                    "CREDENTIAL_PERMISSIONS",
                    "Cloud Agent token file permissions must be owner-only",
                )
            if info.st_size > 8192:
                raise AgentTransportError("CREDENTIAL_INVALID", "Cloud Agent token file is too large")
            token = path.read_text(encoding="utf-8").strip()
        except AgentTransportError:
            raise
        except (OSError, UnicodeError) as exc:
            raise AgentTransportError("CREDENTIAL_UNAVAILABLE", "Cloud Agent token file is unreadable") from exc
    if len(token) < 16 or len(token) > 8192 or any(character.isspace() for character in token):
        raise AgentTransportError("CREDENTIAL_INVALID", "Cloud Agent token is invalid")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _pure_json(value: Any, *, maximum: int | None = None) -> tuple[Any, bytes]:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        decoded = json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentTransportError("INVALID_MESSAGE", "Transport message must be finite JSON") from exc
    if maximum is not None and len(encoded) > maximum:
        raise AgentTransportError("REQUEST_TOO_LARGE", "Cloud Agent request exceeds its configured limit")
    return decoded, encoded


class _FileBody(Iterable[bytes]):
    def __init__(self, path: Path, chunk_size: int = 64 * 1024):
        self.path = path
        self.chunk_size = chunk_size

    def __iter__(self):
        with self.path.open("rb") as handle:
            while True:
                chunk = handle.read(self.chunk_size)
                if not chunk:
                    return
                yield chunk


class AgentHttpClient:
    """Small urllib client using the OS/Python system trust store."""

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        opener: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if config.mode != "cloud_agent" or not config.agent_url:
            raise AgentTransportError("CONFIG_INVALID", "Cloud Agent transport requires cloud_agent mode")
        self.config = config
        self._token = load_bearer_token(config.agent_token_file)
        # This non-secret, process-local identity binds approvals to the exact
        # authenticated credential.  Rotating a token invalidates outstanding
        # approvals without exposing the token to the Palette or controller.
        self.credential_subject = "bearer-sha256:" + hashlib.sha256(self._token.encode("utf-8")).hexdigest()
        self._sleeper = sleeper
        if opener is None:
            context = ssl.create_default_context()
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=context),
                _NoRedirect(),
            )
        self._opener = opener

    def __repr__(self) -> str:
        return f"AgentHttpClient(agent_url={self.config.agent_url!r})"

    def plan(self, body: dict[str, Any]) -> dict[str, Any]:
        request_id = self._request_id(body)
        return self._json_request("POST", PLAN_PATH, body, idempotency_key=request_id, retry=True)

    def capabilities(self) -> dict[str, Any]:
        last_error: AgentTransportError | None = None
        for attempt in range(self.config.retry_attempts):
            request = urllib.request.Request(
                self.config.agent_url + CAPABILITIES_PATH,
                method="GET",
                headers=self._headers(),
            )
            try:
                return self._open_json(request)
            except AgentTransportError as exc:
                last_error = exc
                if not exc.retryable or attempt + 1 >= self.config.retry_attempts:
                    raise
                self._sleeper(self._retry_delay(exc, attempt))
        raise last_error or AgentTransportError(
            "NETWORK_UNAVAILABLE", "Cloud Agent capability negotiation failed", retryable=True
        )

    def report(self, body: dict[str, Any]) -> dict[str, Any]:
        request_id = self._request_id(body)
        return self._json_request("POST", REPORT_PATH, body, idempotency_key=request_id, retry=True)

    def heartbeat(self, body: dict[str, Any]) -> dict[str, Any]:
        connector_id = body.get("connector_instance_id") if isinstance(body, dict) else None
        try:
            connector_id = str(uuid.UUID(str(connector_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise AgentTransportError("INVALID_MESSAGE", "Heartbeat connector ID must be a UUID") from exc
        return self._json_request(
            "POST", HEARTBEAT_PATH, body, idempotency_key=connector_id, retry=False
        )

    def upload_artifact(self, path: Path | str, consent: dict[str, Any]) -> dict[str, Any]:
        authorization, _ = _pure_json(consent, maximum=16 * 1024)
        if set(authorization) != _UPLOAD_FIELDS:
            raise AgentTransportError("UPLOAD_NOT_AUTHORIZED", "Artifact upload consent has invalid fields")
        if authorization.get("upload_authorized") is not True:
            raise AgentTransportError("UPLOAD_NOT_AUTHORIZED", "Artifact upload was not explicitly authorized")
        if authorization.get("format") == "f3d" and authorization.get("f3d_upload_authorized") is not True:
            raise AgentTransportError("UPLOAD_NOT_AUTHORIZED", "F3D upload requires separate authorization")
        if authorization.get("format") != "f3d" and authorization.get("f3d_upload_authorized") is True:
            raise AgentTransportError("UPLOAD_NOT_AUTHORIZED", "F3D authorization cannot be reused")
        request_id = self._request_id(authorization)
        try:
            uuid.UUID(str(authorization.get("artifact_id")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise AgentTransportError("UPLOAD_NOT_AUTHORIZED", "Artifact ID is invalid") from exc
        fmt = authorization.get("format")
        if fmt not in {"step", "stl", "dxf", "f3d", "png"}:
            raise AgentTransportError("UPLOAD_NOT_AUTHORIZED", "Artifact format is invalid")
        expected_digest = authorization.get("sha256")
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(character not in "0123456789abcdef" for character in expected_digest)
        ):
            raise AgentTransportError("UPLOAD_NOT_AUTHORIZED", "Artifact digest claim is invalid")

        source = Path(path).expanduser().resolve()
        root = self.config.artifact_root.expanduser().resolve()
        if not source.is_file() or not source.is_relative_to(root) or source.name != authorization.get("filename"):
            raise AgentTransportError("UPLOAD_NOT_AUTHORIZED", "Artifact path does not match the upload consent")
        size = source.stat().st_size
        if size <= 0 or size > self.config.max_artifact_bytes or size != authorization.get("size_bytes"):
            raise AgentTransportError("ARTIFACT_INVALID", "Artifact size does not match the upload consent")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest() != authorization.get("sha256"):
            raise AgentTransportError("ARTIFACT_INVALID", "Artifact digest does not match the upload consent")
        filename = self._safe_segment(authorization.get("filename"), "filename")
        endpoint = ARTIFACT_PATH.format(
            request_id=urllib.parse.quote(request_id, safe=""),
            filename=urllib.parse.quote(filename, safe=""),
        )
        headers = self._headers()
        headers.update({
            "Content-Type": {
                "step": "model/step", "stl": "model/stl", "dxf": "image/vnd.dxf",
                "f3d": "application/vnd.autodesk.fusion360", "png": "image/png",
            }.get(str(authorization["format"]), "application/octet-stream"),
            "Content-Length": str(size),
            "X-Artifact-Id": str(authorization["artifact_id"]),
            "X-Artifact-Sha256": str(authorization["sha256"]),
            "X-Artifact-Size": str(size),
        })
        request = urllib.request.Request(
            self.config.agent_url + endpoint,
            data=_FileBody(source),
            method="PUT",
            headers=headers,
        )
        # Artifact bodies are intentionally not automatically replayed.  The
        # user-bound authorization and server audit must be reconciled first.
        return self._open_json(request)

    @staticmethod
    def _request_id(body: dict[str, Any]) -> str:
        value = body.get("request_id") if isinstance(body, dict) else None
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise AgentTransportError("INVALID_MESSAGE", "Cloud Agent message request_id must be a UUID") from exc

    @staticmethod
    def _safe_segment(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise AgentTransportError("UPLOAD_NOT_AUTHORIZED", f"Artifact {name} is invalid")
        return value

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "CAD-Agent-Fusion-Connector/1.0.0",
        }

    def _json_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
        retry: bool,
    ) -> dict[str, Any]:
        _, encoded = _pure_json(body, maximum=self.config.max_request_bytes)
        headers = self._headers()
        headers.update({
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
            "Idempotency-Key": idempotency_key,
        })
        attempts = self.config.retry_attempts if retry else 1
        for attempt in range(attempts):
            request = urllib.request.Request(
                self.config.agent_url + path,
                data=encoded,
                method=method,
                headers=headers,
            )
            try:
                return self._open_json(request)
            except AgentTransportError as exc:
                if not exc.retryable or attempt + 1 >= attempts:
                    raise
                self._sleeper(self._retry_delay(exc, attempt))
        raise AgentTransportError("NETWORK_UNAVAILABLE", "Cloud Agent request failed", retryable=True)

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener.open(request, timeout=self.config.request_timeout_s) as response:
                status = int(getattr(response, "status", 200))
                if status < 200 or status >= 300:
                    raise AgentTransportError("HTTP_ERROR", "Cloud Agent rejected the request", retryable=status in _RETRYABLE_HTTP)
                raw = response.read(self.config.max_response_bytes + 1)
                if len(raw) > self.config.max_response_bytes:
                    raise AgentTransportError("RESPONSE_TOO_LARGE", "Cloud Agent response exceeds its configured limit")
                content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
                if content_type and "application/json" not in content_type.lower():
                    raise AgentTransportError("INVALID_RESPONSE", "Cloud Agent response is not JSON")
                try:
                    decoded = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise AgentTransportError("INVALID_RESPONSE", "Cloud Agent returned invalid JSON") from exc
                if not isinstance(decoded, dict):
                    raise AgentTransportError("INVALID_RESPONSE", "Cloud Agent response must be a JSON object")
                return decoded
        except AgentTransportError:
            raise
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise AgentTransportError("REDIRECT_REJECTED", "Cloud Agent redirects are not allowed") from exc
            raise AgentTransportError(
                "HTTP_ERROR",
                f"Cloud Agent request failed with HTTP status {exc.code}",
                retryable=exc.code in _RETRYABLE_HTTP,
                retry_after_s=self._retry_after(exc.headers.get("Retry-After") if exc.headers else None),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Never include raw exception text: urllib reasons can contain a URL
            # or credential supplied by a hostile proxy/test double.
            raise AgentTransportError("NETWORK_UNAVAILABLE", "Cloud Agent is unavailable", retryable=True) from exc

    @staticmethod
    def _retry_after(value: Any) -> float | None:
        if value is None:
            return None
        text = str(value).strip()
        try:
            seconds = float(text)
        except ValueError:
            try:
                moment = parsedate_to_datetime(text)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                seconds = (moment - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        return max(0.0, min(seconds, 30.0))

    @staticmethod
    def _retry_delay(error: AgentTransportError, attempt: int) -> float:
        if error.retry_after_s is not None:
            return error.retry_after_s
        return min(2.0, 0.1 * (2 ** attempt))


class CloudAgentWorker:
    """A pure-JSON queue worker; it never owns a Fusion facade or Palette."""

    def __init__(
        self,
        config: ConnectorConfig,
        inbound: queue.Queue,
        outbound: queue.Queue,
        signal_main_thread: Callable[[], None],
        *,
        client: AgentHttpClient | None = None,
        registration: dict[str, Any] | None = None,
    ):
        if inbound.maxsize <= 0 or outbound.maxsize <= 0:
            raise AgentTransportError("CONFIG_INVALID", "Cloud Agent worker queues must be bounded")
        self.config = config
        self.inbound = inbound
        self.outbound = outbound
        self.signal_main_thread = signal_main_thread
        self.client = client or AgentHttpClient(config)
        self.registration = _pure_json(registration)[0] if registration is not None else None
        self._negotiated = registration is None
        self._negotiation_error: AgentTransportError | None = None
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._accepting = True

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        if self.stop_event.is_set():
            raise AgentTransportError("WORKER_STOPPED", "Cloud Agent worker cannot be restarted after stop")
        self.thread = threading.Thread(target=self._run, name="CADAgentCloudTransport", daemon=True)
        self.thread.start()

    def submit(self, message: dict[str, Any]) -> bool:
        if not self._accepting or self.stop_event.is_set():
            return False
        pure, encoded = _pure_json(message, maximum=self.config.max_request_bytes)
        if not isinstance(pure, dict):
            raise AgentTransportError("INVALID_MESSAGE", "Worker message must be a JSON object")
        try:
            self.inbound.put_nowait(pure)
            return True
        except queue.Full:
            return False

    def stop(self, timeout: float = 3.0) -> bool:
        self._accepting = False
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=max(0.0, timeout))
            return not self.thread.is_alive()
        return True

    def _run(self) -> None:
        last_heartbeat = 0.0
        last_negotiation_attempt = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if not self._negotiated and now - last_negotiation_attempt >= 5.0:
                last_negotiation_attempt = now
                try:
                    offered = self.client.capabilities()
                    versions = offered.get("contract_versions") if isinstance(offered, dict) else None
                    actions = offered.get("actions") if isinstance(offered, dict) else None
                    required_actions = set(self.registration["capabilities"].get("actions", []))
                    if (
                        not isinstance(versions, list)
                        or "1.0.0" not in versions
                        or not isinstance(actions, list)
                        or not required_actions.issubset(set(actions))
                    ):
                        raise AgentTransportError(
                            "PROTOCOL_MISMATCH",
                            "Cloud Agent contract or action capabilities are incompatible",
                        )
                    self._negotiated = True
                    self._negotiation_error = None
                except AgentTransportError as exc:
                    self._negotiation_error = exc
            if (
                self._negotiated
                and
                self.registration is not None
                and self.inbound.empty()
                and now - last_heartbeat >= 5.0
            ):
                heartbeat = {
                    "contract_version": "1.0.0",
                    "connector_instance_id": self.registration["connector_instance_id"],
                    "fusion_version": self.registration["fusion_version"],
                    "addin_version": self.registration["addin_version"],
                    "platform": self.registration["platform"],
                    "capabilities": self.registration["capabilities"],
                    "sent_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                try:
                    self.client.heartbeat(heartbeat)
                except AgentTransportError:
                    # Heartbeat failure is observed by server TTL. User requests
                    # still receive their own structured transport error.
                    log.warning("Cloud Agent heartbeat unavailable")
                last_heartbeat = now
            try:
                message = self.inbound.get(timeout=0.1)
            except queue.Empty:
                continue
            request_id = ""
            kind = message.get("kind") if isinstance(message, dict) else None
            payload = message.get("payload") if isinstance(message, dict) else None
            if isinstance(payload, dict):
                request_id = str(payload.get("request_id", ""))
            try:
                if not self._negotiated:
                    raise self._negotiation_error or AgentTransportError(
                        "PROTOCOL_MISMATCH", "Cloud Agent contract negotiation is incomplete"
                    )
                if kind == "plan" and isinstance(payload, dict):
                    response = self.client.plan(payload)
                    outgoing = {
                        "kind": "plan_result", "request_id": request_id, "payload": response,
                        "credential_subject": self.client.credential_subject,
                    }
                elif kind == "report" and isinstance(payload, dict):
                    response = self.client.report(payload)
                    outgoing = {"kind": "report_result", "request_id": request_id, "payload": response}
                elif kind == "artifact" and isinstance(payload, dict):
                    if set(payload) != {"request_id", "path", "consent"}:
                        raise AgentTransportError("INVALID_MESSAGE", "Artifact worker message has invalid fields")
                    response = self.client.upload_artifact(payload["path"], payload["consent"])
                    outgoing = {"kind": "artifact_result", "request_id": request_id, "payload": response}
                else:
                    raise AgentTransportError("INVALID_MESSAGE", "Unsupported Cloud Agent worker operation")
            except AgentTransportError as exc:
                outgoing = {
                    "kind": "transport_error",
                    "request_id": request_id,
                    "operation": kind if isinstance(kind, str) else "unknown",
                    "error": exc.as_dict(),
                }
            except Exception:
                # Raw exception text can contain proxy URLs or credential data.
                log.error("Unexpected Cloud Agent transport failure")
                outgoing = {
                    "kind": "transport_error",
                    "request_id": request_id,
                    "operation": kind if isinstance(kind, str) else "unknown",
                    "error": AgentTransportError(
                        "NETWORK_UNAVAILABLE", "Cloud Agent request failed", retryable=True
                    ).as_dict(),
                }
            if self.stop_event.is_set():
                continue
            # The HTTP payload is already bounded by max_response_bytes; allow
            # only the small fixed worker envelope in addition to it.
            pure, _ = _pure_json(outgoing, maximum=self.config.max_response_bytes + 16 * 1024)
            while not self.stop_event.is_set():
                try:
                    self.outbound.put(pure, timeout=0.1)
                    self.signal_main_thread()
                    break
                except queue.Full:
                    continue


# Compatibility-friendly explicit name for callers that prefer the generic
# transport terminology.
AgentTransportWorker = CloudAgentWorker
