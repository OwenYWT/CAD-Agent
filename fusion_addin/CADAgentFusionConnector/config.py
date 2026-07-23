"""Dependency-free Add-in configuration loading and validation.

Cloud Agent mode is the default.  The previous loopback Runtime remains an
explicit compatibility mode so deployments can switch transports without
changing the Fusion API adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


_MODES = {"cloud_agent", "local_runtime"}
_CONFIG_FIELDS = {
    "mode", "agent_url", "agent_token_file", "runtime_url", "connector_secret",
    "connector_secret_file", "connector_instance_id", "artifact_root", "journal_path",
    "poll_interval_s", "request_timeout_s", "max_request_bytes", "max_response_bytes",
    "max_artifact_bytes", "queue_size", "retry_attempts", "approval_ttl_s",
}


@dataclass(frozen=True)
class ConnectorConfig:
    connector_instance_id: str
    artifact_root: Path
    journal_path: Path
    mode: str = "cloud_agent"
    agent_url: str | None = None
    agent_token_file: Path | None = None
    runtime_url: str | None = None
    connector_secret: str = ""
    poll_interval_s: float = 0.25
    request_timeout_s: float = 10.0
    max_request_bytes: int = 1024 * 1024
    max_response_bytes: int = 1024 * 1024
    max_artifact_bytes: int = 64 * 1024 * 1024
    queue_size: int = 16
    retry_attempts: int = 3
    approval_ttl_s: float = 600.0

    @property
    def artifact_root_fingerprint(self) -> str:
        return hashlib.sha256(str(self.artifact_root.expanduser().resolve()).encode("utf-8")).hexdigest()

    @property
    def platform_name(self) -> str:
        return "windows" if platform.system().lower().startswith("win") else "macos"


def default_config_path() -> Path:
    configured = os.environ.get("CAD_AGENT_FUSION_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cad-agent" / "fusion360" / "connector.json"


def _validated_url(value: object, *, cloud: bool) -> str:
    url = str(value or "").rstrip("/")
    parsed = urlparse(url)
    loopback = parsed.hostname in {"127.0.0.1", "::1"}
    valid_scheme = parsed.scheme == "https" or (cloud and parsed.scheme == "http" and loopback)
    if not cloud:
        valid_scheme = parsed.scheme == "http" and loopback
    if (
        not valid_scheme
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        target = "Cloud Agent URL must use HTTPS (or explicit loopback HTTP for development)"
        if not cloud:
            target = "Fusion Runtime URL must use loopback HTTP"
        raise RuntimeError(target)
    # A Runtime URL is a bare origin.  Agent deployments may use a reverse-proxy
    # prefix, but dot segments and a trailing endpoint-looking filename are not
    # accepted.
    if not cloud and parsed.path not in {"", "/"}:
        raise RuntimeError("Fusion Runtime URL must use loopback HTTP")
    if cloud and any(segment in {".", ".."} for segment in parsed.path.split("/")):
        raise RuntimeError("Cloud Agent URL contains an ambiguous path")
    return url


def _bounded_number(raw: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be an integer") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{key} must be between {minimum} and {maximum}")
    return value


def load_config(path: Path | None = None) -> ConnectorConfig:
    path = (path or default_config_path()).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"Fusion connector config is missing: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Fusion connector config is unreadable or invalid JSON") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Fusion connector config must be a JSON object")
    unknown = set(raw).difference(_CONFIG_FIELDS)
    if unknown:
        raise RuntimeError("Fusion connector config contains unknown fields")

    mode = str(raw.get("mode", "cloud_agent"))
    if mode not in _MODES:
        raise RuntimeError("Fusion connector mode must be cloud_agent or local_runtime")

    agent_url: str | None = None
    agent_token_file: Path | None = None
    runtime_url: str | None = None
    secret = ""
    if mode == "cloud_agent":
        agent_url = _validated_url(raw.get("agent_url"), cloud=True)
        token_file = raw.get("agent_token_file")
        if token_file:
            agent_token_file = Path(str(token_file)).expanduser().resolve()
        elif not os.environ.get("CAD_AGENT_FUSION_TOKEN"):
            raise RuntimeError("Cloud Agent token file or CAD_AGENT_FUSION_TOKEN is required")
    else:
        runtime_url = _validated_url(raw.get("runtime_url"), cloud=False)
        secret = os.environ.get("FUSION_RUNTIME_CONNECTOR_SECRET") or str(raw.get("connector_secret", ""))
        secret_file = raw.get("connector_secret_file")
        if not secret and secret_file:
            try:
                secret = Path(str(secret_file)).expanduser().read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise RuntimeError("Fusion Runtime connector secret file is unreadable") from exc
        if len(secret) < 32:
            raise RuntimeError("Fusion connector secret must contain at least 32 characters")

    artifact_value = str(raw.get("artifact_root", ""))
    if not artifact_value:
        raise RuntimeError("Fusion artifact_root is required")
    root = Path(artifact_value).expanduser().resolve()
    connector_id = str(raw.get("connector_instance_id", ""))
    try:
        uuid.UUID(connector_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("connector_instance_id must be a stable UUID") from exc
    journal = Path(raw.get("journal_path") or path.with_name("execution-journal.json")).expanduser().resolve()
    try:
        poll = float(raw.get("poll_interval_s", 0.25))
        timeout = float(raw.get("request_timeout_s", 10.0))
        approval_ttl = float(raw.get("approval_ttl_s", 600.0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Fusion connector timing values must be numeric") from exc
    return ConnectorConfig(
        connector_instance_id=connector_id,
        artifact_root=root,
        journal_path=journal,
        mode=mode,
        agent_url=agent_url,
        agent_token_file=agent_token_file,
        runtime_url=runtime_url,
        connector_secret=secret,
        poll_interval_s=max(0.05, min(poll, 5.0)),
        request_timeout_s=max(1.0, min(timeout, 60.0)),
        max_request_bytes=_bounded_number(raw, "max_request_bytes", 1024 * 1024, 1024, 16 * 1024 * 1024),
        max_response_bytes=_bounded_number(raw, "max_response_bytes", 1024 * 1024, 1024, 16 * 1024 * 1024),
        max_artifact_bytes=_bounded_number(raw, "max_artifact_bytes", 64 * 1024 * 1024, 1024, 4 * 1024 * 1024 * 1024),
        queue_size=_bounded_number(raw, "queue_size", 16, 1, 256),
        retry_attempts=_bounded_number(raw, "retry_attempts", 3, 1, 5),
        approval_ttl_s=max(30.0, min(approval_ttl, 900.0)),
    )
