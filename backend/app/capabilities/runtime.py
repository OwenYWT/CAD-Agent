"""Safe, typed dispatcher for the vendored cadskills command-line tools.

This module is intentionally not a generic command runner.  Public callers
select a known ``capability`` and ``action`` and provide a small typed parameter
object.  Commands, script paths, network origins and output roots are owned by
the server.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .artifacts import ArtifactPathError, ArtifactStore, sha256_file


class CapabilityRequestError(ValueError):
    """A safe user-facing validation error."""


@dataclass(frozen=True)
class RuntimeConfig:
    repo_root: Path | str | None = None
    workspace_root: Path | str | None = None
    artifact_root: Path | str | None = None
    timeout_seconds: float = 120.0
    network_timeout_seconds: float = 30.0
    max_output_bytes: int = 256 * 1024
    max_fetch_bytes: int = 12 * 1024 * 1024
    # Deployment-level device-network gate.  API params cannot override it.
    allow_bambu_lan: bool = False
    # A deployment-owned prefix, for example a locked-down container runner.
    # It must not come from an API request.  Python/JS generators are blocked
    # unless this is configured.
    isolated_executor: tuple[str, ...] | None = None


_CAPABILITY_ALIASES = {
    "bambu": "bambu-labs",
    "bambu_labs": "bambu-labs",
    "cad_viewer": "cad-viewer",
    "implicit": "implicit-cad",
    "implicit_cad": "implicit-cad",
    "step_parts": "step-parts",
    "send-cut-send": "sendcutsend",
}

_OFFICIAL_SENDCUTSEND_SOURCES = {
    "ordering-guide": "https://cdn.sendcutsend.com/specs/sendcutsend-ordering-guide.md",
    "catalog": "https://cdn.sendcutsend.com/specs/sendcutsend-catalog.json",
    "specs": "https://cdn.sendcutsend.com/specs/sendcutsend-specs.json",
}

_MISSING_DEPENDENCY_MARKERS = (
    "modulenotfounderror",
    "no module named",
    "importerror",
    "cannot import name",
    "cannot find package",
    "command not found",
    "executable not found",
    "could not find executable",
    "is not installed",
    "playwright install",
    "browser executable",
)


def _plain_params(params: Mapping[str, Any] | object | None) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, Mapping):
        return dict(params)
    model_dump = getattr(params, "model_dump", None)
    if callable(model_dump):
        value = model_dump(exclude_none=True)
        if isinstance(value, Mapping):
            return dict(value)
    raise CapabilityRequestError("params must be an object")


def _validate_keys(
    params: Mapping[str, Any],
    allowed: set[str],
    *,
    required: set[str] | None = None,
) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise CapabilityRequestError(f"unsupported parameter(s): {', '.join(unknown)}")
    missing = sorted((required or set()) - set(params))
    if missing:
        raise CapabilityRequestError(f"missing required parameter(s): {', '.join(missing)}")


def _text(
    value: object,
    name: str,
    *,
    max_length: int = 256,
    pattern: str | None = None,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise CapabilityRequestError(f"{name} must be a string")
    result = value.strip()
    if not result and not allow_empty:
        raise CapabilityRequestError(f"{name} must not be empty")
    if len(result) > max_length or any(ord(char) < 32 for char in result):
        raise CapabilityRequestError(f"{name} contains invalid or excessive text")
    if pattern and not re.fullmatch(pattern, result):
        raise CapabilityRequestError(f"{name} has an invalid format")
    return result


def _boolean(value: object, name: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise CapabilityRequestError(f"{name} must be a boolean")
    return value


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise CapabilityRequestError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _number(value: object, name: str, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CapabilityRequestError(f"{name} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise CapabilityRequestError(f"{name} must be between {minimum} and {maximum}")
    return result


def _choice(value: object, name: str, choices: set[str], *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    result = _text(value, name, max_length=64)
    if result not in choices:
        raise CapabilityRequestError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return result


def _string_list(value: object, name: str, *, maximum: int = 20) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise CapabilityRequestError(f"{name} must be a list with at most {maximum} values")
    return [_text(item, f"{name} item", max_length=160) for item in value]


def _safe_filename(value: object, name: str, *, suffixes: set[str] | None = None) -> str:
    result = _text(value, name, max_length=180)
    if result.startswith("-") or Path(result).name != result or "/" in result or "\\" in result:
        raise CapabilityRequestError(f"{name} must be a plain filename")
    if suffixes and not any(result.lower().endswith(suffix) for suffix in suffixes):
        raise CapabilityRequestError(f"{name} must end with one of: {', '.join(sorted(suffixes))}")
    return result


class CapabilityRuntime:
    """Dispatch allowlisted cadskills actions with path and execution controls."""

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        default_repo = Path(__file__).resolve().parents[3]
        self.repo_root = Path(self.config.repo_root or default_repo).expanduser().resolve()
        self.workspace_root = Path(self.config.workspace_root or self.repo_root).expanduser().resolve()
        artifact_root = Path(
            self.config.artifact_root or self.repo_root / "backend" / "data" / "capability_artifacts"
        )
        self.artifacts = artifact_store or ArtifactStore(artifact_root, workspace_root=self.workspace_root)
        self.vendor_root = self.repo_root / "third_party" / "cadskills" / "skills"
        self._dispatch: dict[tuple[str, str], Callable[[dict[str, Any], str], dict[str, Any]]] = {
            ("step-parts", "search"): self._step_parts_search,
            ("step-parts", "download"): self._step_parts_download,
            ("gcode", "discover"): self._gcode_discover,
            ("gcode", "inspect"): self._gcode_inspect,
            ("gcode", "dry-run"): self._gcode_dry_run,
            ("gcode", "slice"): self._gcode_slice,
            ("gcode", "validate"): self._gcode_validate,
            ("bambu-labs", "serial"): self._bambu_serial,
            ("bambu-labs", "status"): self._bambu_status,
            ("bambu-labs", "dry-run"): self._bambu_dry_run,
            ("bambu-labs", "send"): self._bambu_send,
            ("bambu-labs", "upload"): lambda p, r: self._alias_result("upload", self._bambu_send({**p, "send_action": "upload"}, r)),
            ("bambu-labs", "start-print"): lambda p, r: self._alias_result(
                "start-print",
                self._bambu_send(
                    {**p, "send_action": "start", "confirm_send": p.get("confirm_start_print", False)}, r
                ),
            ),
            ("bambu-labs", "pause"): self._bambu_pause,
            ("bambu-labs", "pause-print"): lambda p, r: self._alias_result("pause-print", self._bambu_pause(p, r)),
            ("bambu-labs", "cancel"): self._bambu_cancel,
            ("bambu-labs", "cancel-print"): lambda p, r: self._alias_result("cancel-print", self._bambu_cancel(p, r)),
            ("bambu-labs", "clear-error"): self._bambu_clear_error,
            ("urdf", "generate"): lambda p, r: self._xml_generator("urdf", p, r),
            ("srdf", "generate"): lambda p, r: self._xml_generator("srdf", p, r),
            ("sdf", "generate"): lambda p, r: self._xml_generator("sdf", p, r),
            ("sdf", "gz-check"): self._sdf_gz_check,
            ("implicit-cad", "export"): self._implicit_export,
            ("implicit-cad", "snapshot"): self._implicit_snapshot,
            ("cad", "step"): self._cad_step,
            ("cad", "generate"): lambda p, r: self._alias_result("generate", self._cad_step(p, r)),
            ("cad", "export"): self._cad_export,
            ("cad", "inspect"): self._cad_inspect,
            ("cad", "snapshot"): self._cad_snapshot,
            ("dxf", "generate"): self._dxf_generate,
            ("dxf", "validate"): self._dxf_validate,
            ("cad-viewer", "status"): self._viewer_status,
            ("cad-viewer", "start"): self._viewer_start,
            ("cad-viewer", "review"): self._viewer_review,
            ("sendcutsend", "fetch"): self._sendcutsend_fetch,
            ("sendcutsend", "preflight"): self._sendcutsend_preflight,
        }

    def execute(
        self,
        capability: str,
        action: str,
        params: Mapping[str, Any] | object | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request = request_id or uuid.uuid4().hex
        try:
            request = self.artifacts.validate_request_id(request)
            capability_id = _CAPABILITY_ALIASES.get(str(capability).strip(), str(capability).strip())
            action_id = str(action).strip()
            handler = self._dispatch.get((capability_id, action_id))
            if handler is None:
                return self._base(
                    capability_id,
                    action_id,
                    request,
                    status="blocked",
                    blocked_reasons=["Unknown capability/action; arbitrary commands are not supported."],
                )
            return handler(_plain_params(params), request)
        except (CapabilityRequestError, ArtifactPathError) as exc:
            return self._base(str(capability), str(action), request, status="failed", error=str(exc))
        except FileNotFoundError:
            return self._base(
                str(capability),
                str(action),
                request,
                status="blocked",
                blocked_reasons=["Required vendored tool or external dependency is unavailable."],
            )
        except Exception as exc:  # preserve the structured API boundary
            return self._base(
                str(capability),
                str(action),
                request,
                status="failed",
                error=f"capability runtime error: {type(exc).__name__}",
            )

    @staticmethod
    def _base(
        capability: str,
        action: str,
        request_id: str,
        *,
        status: str,
        data: Any = None,
        files: list[dict[str, Any]] | None = None,
        checks: list[dict[str, Any]] | None = None,
        command_preview: list[str] | None = None,
        blocked_reasons: list[str] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": status,
            "capability": capability,
            "action": action,
            "request_id": request_id,
            "data": data,
            "files": files or [],
            "checks": checks or [],
            "command_preview": command_preview or [],
            "blocked_reasons": blocked_reasons or [],
        }
        if error:
            result["error"] = error
        return result

    @staticmethod
    def _alias_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
        result["action"] = action
        return result

    def _script(self, capability: str, *parts: str) -> Path:
        path = (self.vendor_root / capability / "scripts" / Path(*parts)).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _input(self, value: object, *, suffixes: set[str]) -> Path:
        return self.artifacts.workspace_path(value, suffixes=suffixes)

    @staticmethod
    def _truncate(value: bytes | str | None, limit: int) -> tuple[str, bool]:
        raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else (value or b"")
        truncated = len(raw) > limit
        text = raw[:limit].decode("utf-8", errors="replace")
        return text, truncated

    @staticmethod
    def _redact_text(value: str, secrets: Sequence[str]) -> str:
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result

    @classmethod
    def _preview(cls, command: Sequence[str], secrets: Sequence[str]) -> list[str]:
        return [cls._redact_text(str(part), secrets) for part in command]

    def _run(
        self,
        capability: str,
        action: str,
        request_id: str,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
        secrets: Sequence[str] = (),
        status_on_success: str = "succeeded",
    ) -> dict[str, Any]:
        preview = self._preview(command, secrets)
        try:
            completed = subprocess.run(
                [str(part) for part in command],
                cwd=str(cwd or self.workspace_root),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.config.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return self._base(
                capability,
                action,
                request_id,
                status="blocked",
                command_preview=preview,
                blocked_reasons=["Required executable or vendored tool is not installed."],
            )
        except subprocess.TimeoutExpired:
            return self._base(
                capability,
                action,
                request_id,
                status="failed",
                command_preview=preview,
                error="capability execution timed out",
            )

        stdout, stdout_truncated = self._truncate(completed.stdout, self.config.max_output_bytes)
        stderr, stderr_truncated = self._truncate(completed.stderr, self.config.max_output_bytes)
        stdout = self._redact_text(stdout, secrets)
        stderr = self._redact_text(stderr, secrets)
        try:
            parsed_stdout: Any = json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError:
            parsed_stdout = stdout
        data = {
            "exit_code": completed.returncode,
            "result": parsed_stdout,
            "stderr": stderr or None,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        files = self.artifacts.list_request(request_id)
        if completed.returncode == 0:
            return self._base(
                capability,
                action,
                request_id,
                status=status_on_success,
                data=data,
                files=files,
                command_preview=preview,
            )
        combined = f"{stdout}\n{stderr}".lower()
        if any(marker in combined for marker in _MISSING_DEPENDENCY_MARKERS):
            return self._base(
                capability,
                action,
                request_id,
                status="blocked",
                data=data,
                files=files,
                command_preview=preview,
                blocked_reasons=["An optional runtime dependency is unavailable."],
            )
        return self._base(
            capability,
            action,
            request_id,
            status="failed",
            data=data,
            files=files,
            command_preview=preview,
            error="vendored capability command failed",
        )

    def _generator_run(
        self,
        capability: str,
        action: str,
        request_id: str,
        command: Sequence[str],
    ) -> dict[str, Any]:
        prefix = self.config.isolated_executor
        preview = self._preview(command, ())
        if not prefix:
            return self._base(
                capability,
                action,
                request_id,
                status="blocked",
                command_preview=preview,
                blocked_reasons=[
                    "Generator execution is disabled on the host; configure a deployment-owned isolated executor."
                ],
            )
        return self._run(capability, action, request_id, [*prefix, *command])

    # -- step.parts -----------------------------------------------------

    def _step_parts_command(self, params: dict[str, Any], request_id: str, *, download: bool) -> list[str]:
        allowed = {"query", "part_id", "limit", "page", "tags", "categories", "families", "standards"}
        if download:
            allowed |= {"filename", "all"}
        _validate_keys(params, allowed)
        query = params.get("query")
        part_id = params.get("part_id")
        if not query and not part_id and not any(params.get(key) for key in ("tags", "categories", "families", "standards")):
            raise CapabilityRequestError("provide query, part_id, or at least one facet")
        command = [sys.executable, str(self._script("step-parts", "download_step_part.py"))]
        if query:
            query_text = _text(query, "query", max_length=240)
            if query_text.startswith("-"):
                raise CapabilityRequestError("query must not begin with an option prefix")
            command.append(query_text)
        if part_id:
            command.extend(["--id", _text(part_id, "part_id", max_length=160, pattern=r"[A-Za-z0-9_.:-]+")])
        command.extend(["--limit", str(_integer(params.get("limit", 10), "limit", minimum=1, maximum=100))])
        command.extend(["--page", str(_integer(params.get("page", 1), "page", minimum=1, maximum=10000))])
        for key, flag in (("tags", "--tag"), ("categories", "--category"), ("families", "--family"), ("standards", "--standard")):
            for value in _string_list(params.get(key), key):
                if value.startswith("-"):
                    raise CapabilityRequestError(f"{key} values must not begin with an option prefix")
                command.extend([flag, value])
        if download:
            command.extend(["--download", "--out-dir", str(self.artifacts.request_dir(request_id))])
            if _boolean(params.get("all"), "all"):
                command.append("--all")
            if params.get("filename"):
                command.extend(["--filename", _safe_filename(params["filename"], "filename", suffixes={".step", ".stp"})])
        return command

    def _step_parts_search(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        return self._run("step-parts", "search", request_id, self._step_parts_command(params, request_id, download=False))

    def _step_parts_download(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        return self._run("step-parts", "download", request_id, self._step_parts_command(params, request_id, download=True))

    # -- G-code --------------------------------------------------------

    def _gcode_script(self) -> str:
        return str(self._script("gcode", "gcode_tool.py"))

    def _gcode_discover(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, set())
        return self._run("gcode", "discover", request_id, [sys.executable, self._gcode_script(), "discover"])

    def _gcode_inspect(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, {"input"}, required={"input"})
        input_path = self._input(params["input"], suffixes={".stl", ".obj", ".3mf", ".ply", ".glb", ".gltf", ".step", ".stp", ".dxf", ".svg", ".urdf", ".sdf"})
        command = [sys.executable, self._gcode_script(), "inspect", "--input", str(input_path), "--json"]
        return self._run("gcode", "inspect", request_id, command)

    def _gcode_slice_command(self, params: dict[str, Any], request_id: str, *, dry_run: bool) -> list[str]:
        _validate_keys(params, {"input", "profile", "output", "backend"}, required={"input", "profile"})
        input_path = self._input(params["input"], suffixes={".stl", ".obj", ".3mf", ".ply", ".glb", ".gltf"})
        profile = self._input(params["profile"], suffixes={".json"})
        output_name = _safe_filename(params.get("output", "output.gcode"), "output", suffixes={".gcode"})
        output = self.artifacts.output_path(request_id, output_name, suffixes={".gcode"})
        backend = _choice(params.get("backend"), "backend", {"auto", "orcaslicer", "prusa-slicer", "curaengine"}, default="auto")
        return [
            sys.executable,
            self._gcode_script(),
            "slice",
            "--input",
            str(input_path),
            "--output",
            str(output),
            "--profile",
            str(profile),
            "--backend",
            backend,
            "--dry-run" if dry_run else "--execute",
        ]

    def _gcode_dry_run(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        return self._run(
            "gcode",
            "dry-run",
            request_id,
            self._gcode_slice_command(params, request_id, dry_run=True),
            status_on_success="dry_run",
        )

    def _gcode_slice(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        return self._run("gcode", "slice", request_id, self._gcode_slice_command(params, request_id, dry_run=False))

    def _gcode_validate(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, {"gcode", "profile"}, required={"gcode", "profile"})
        gcode = self._input(params["gcode"], suffixes={".gcode"})
        profile = self._input(params["profile"], suffixes={".json"})
        command = [sys.executable, self._gcode_script(), "validate", "--gcode", str(gcode), "--profile", str(profile), "--json"]
        return self._run("gcode", "validate", request_id, command)

    # -- Bambu Lab LAN -------------------------------------------------

    def _bambu_blocked_unless_confirmed(
        self,
        action: str,
        params: Mapping[str, Any],
        request_id: str,
        confirm_names: Sequence[str],
    ) -> dict[str, Any] | None:
        if not self.config.allow_bambu_lan:
            return self._base(
                "bambu-labs",
                action,
                request_id,
                status="blocked",
                blocked_reasons=["Bambu LAN access is disabled by server configuration."],
            )
        execute = _boolean(params.get("execute"), "execute")
        confirmed = any(_boolean(params.get(name), name) for name in confirm_names)
        if execute and confirmed:
            return None
        return self._base(
            "bambu-labs",
            action,
            request_id,
            status="blocked",
            blocked_reasons=[
                f"Live printer action requires execute=true and one confirmation flag: {', '.join(confirm_names)}."
            ],
        )

    def _bambu_common(self, params: Mapping[str, Any], *, include_access_code: bool) -> tuple[list[str], list[str]]:
        command: list[str] = []
        secrets: list[str] = []
        if params.get("config"):
            command.extend(["--config", str(self._input(params["config"], suffixes={".json"}))])
        if params.get("printer"):
            command.extend(["--printer", _text(params["printer"], "printer", max_length=80, pattern=r"[A-Za-z0-9_.-]+")])
        if params.get("host"):
            command.extend(["--host", _text(params["host"], "host", max_length=253, pattern=r"[A-Za-z0-9_.:-]+")])
        if params.get("serial"):
            command.extend(["--serial", _text(params["serial"], "serial", max_length=128, pattern=r"[A-Za-z0-9_-]+")])
        if include_access_code and params.get("access_code"):
            secret = _text(params["access_code"], "access_code", max_length=128, pattern=r"[^\s]+")
            secrets.append(secret)
            command.extend(["--access-code", secret])
        if "mqtt_port" in params:
            command.extend(["--mqtt-port", str(_integer(params["mqtt_port"], "mqtt_port", minimum=1, maximum=65535))])
        if "timeout" in params:
            command.extend(["--timeout", str(_number(params["timeout"], "timeout", minimum=1, maximum=60))])
        if _boolean(params.get("tls_verify"), "tls_verify"):
            command.append("--tls-verify")
        return command, secrets

    def _bambu_serial(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {"execute", "confirm_serial", "confirm_network", "config", "printer", "host", "mqtt_port", "timeout", "tls_verify"}
        _validate_keys(params, allowed)
        blocked = self._bambu_blocked_unless_confirmed("serial", params, request_id, ("confirm_serial", "confirm_network"))
        if blocked:
            return blocked
        common, secrets = self._bambu_common(params, include_access_code=False)
        command = [sys.executable, str(self._script("bambu-labs", "bambu_lan_print.py")), "serial", *common, "--json"]
        return self._run("bambu-labs", "serial", request_id, command, secrets=secrets, timeout=65)

    def _bambu_status(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {
            "execute", "confirm_status", "confirm_network", "config", "printer", "host", "serial", "access_code",
            "mqtt_port", "timeout", "tls_verify", "push_all", "wait_seconds", "max_messages",
        }
        _validate_keys(params, allowed)
        blocked = self._bambu_blocked_unless_confirmed("status", params, request_id, ("confirm_status", "confirm_network"))
        if blocked:
            return blocked
        common, secrets = self._bambu_common(params, include_access_code=True)
        command = [sys.executable, str(self._script("bambu-labs", "bambu_lan_print.py")), "status", *common]
        if _boolean(params.get("push_all"), "push_all"):
            command.append("--push-all")
        if "wait_seconds" in params:
            command.extend(["--wait-seconds", str(_number(params["wait_seconds"], "wait_seconds", minimum=0.1, maximum=30))])
        if "max_messages" in params:
            command.extend(["--max-messages", str(_integer(params["max_messages"], "max_messages", minimum=1, maximum=20))])
        return self._run("bambu-labs", "status", request_id, command, secrets=secrets, timeout=65)

    def _bambu_dry_run(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {
            "config", "printer", "host", "serial", "access_code", "mqtt_port", "timeout", "tls_verify",
            "gcode", "handoff", "remote_name", "plate", "template_project",
        }
        _validate_keys(params, allowed, required={"gcode"})
        gcode = self._input(params["gcode"], suffixes={".gcode"})
        handoff = _choice(
            params.get("handoff"), "handoff", {"plain", "bambox-project", "template-project"}, default="plain"
        )
        if handoff == "template-project" and not params.get("template_project"):
            raise CapabilityRequestError("template-project handoff requires template_project")
        common, secrets = self._bambu_common(params, include_access_code=True)
        command = [
            sys.executable, str(self._script("bambu-labs", "bambu_lan_print.py")), "send",
            "--gcode", str(gcode), "--action", "plan", "--handoff", handoff, *common,
        ]
        if params.get("remote_name"):
            command.extend(["--remote-name", _safe_filename(params["remote_name"], "remote_name")])
        if "plate" in params:
            command.extend(["--plate", str(_integer(params["plate"], "plate", minimum=1, maximum=100))])
        if params.get("template_project"):
            template = self._input(params["template_project"], suffixes={".3mf", ".gcode.3mf"})
            command.extend(["--template-project", str(template)])
        return self._run(
            "bambu-labs", "dry-run", request_id, command, secrets=secrets, status_on_success="dry_run"
        )

    def _bambu_send(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {
            "execute", "confirm_send", "confirm_start_print", "config", "printer", "host", "serial", "access_code",
            "mqtt_port", "timeout", "tls_verify", "gcode", "send_action", "handoff", "remote_name", "plate",
            "template_project",
        }
        _validate_keys(params, allowed, required={"gcode"})
        send_action = _choice(params.get("send_action"), "send_action", {"upload", "start", "upload-start"}, default="upload")
        blocked = self._bambu_blocked_unless_confirmed("send", params, request_id, ("confirm_send",))
        if blocked:
            return blocked
        if send_action in {"start", "upload-start"} and not _boolean(params.get("confirm_start_print"), "confirm_start_print"):
            return self._base(
                "bambu-labs", "send", request_id, status="blocked",
                blocked_reasons=["Starting a print requires confirm_start_print=true in addition to confirm_send=true."],
            )
        gcode = self._input(params["gcode"], suffixes={".gcode"})
        common, secrets = self._bambu_common(params, include_access_code=True)
        handoff = _choice(
            params.get("handoff"),
            "handoff",
            {"plain", "bambox-project", "template-project"},
            default="plain",
        )
        if handoff == "template-project" and not params.get("template_project"):
            raise CapabilityRequestError("template-project handoff requires template_project")
        if handoff != "template-project" and params.get("template_project"):
            raise CapabilityRequestError("template_project is valid only with template-project handoff")
        command = [
            sys.executable,
            str(self._script("bambu-labs", "bambu_lan_print.py")),
            "send",
            "--gcode",
            str(gcode),
            "--action",
            send_action,
            "--handoff",
            handoff,
            *common,
            "--execute",
        ]
        if send_action in {"start", "upload-start"}:
            command.append("--confirm-start-print")
        if params.get("remote_name"):
            command.extend(["--remote-name", _safe_filename(params["remote_name"], "remote_name")])
        if params.get("template_project"):
            template = self._input(params["template_project"], suffixes={".3mf", ".gcode.3mf"})
            command.extend(["--template-project", str(template)])
        if "plate" in params:
            command.extend(["--plate", str(_integer(params["plate"], "plate", minimum=1, maximum=100))])
        return self._run("bambu-labs", "send", request_id, command, secrets=secrets, timeout=300)

    def _bambu_control(self, action: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        confirm_name = {
            "pause": "confirm_pause_print",
            "cancel": "confirm_cancel_print",
            "clear-error": "confirm_clear_error",
        }[action]
        allowed = {
            "execute", confirm_name, "config", "printer", "host", "serial", "access_code",
            "mqtt_port", "timeout", "tls_verify",
        }
        _validate_keys(params, allowed)
        blocked = self._bambu_blocked_unless_confirmed(action, params, request_id, (confirm_name,))
        if blocked:
            return blocked
        common, secrets = self._bambu_common(params, include_access_code=True)
        command = [sys.executable, str(self._script("bambu-labs", "bambu_lan_print.py")), action, *common, "--execute"]
        if action == "cancel":
            command.append("--confirm-cancel-print")
        return self._run("bambu-labs", action, request_id, command, secrets=secrets, timeout=65)

    def _bambu_pause(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        return self._bambu_control("pause", params, request_id)

    def _bambu_cancel(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        return self._bambu_control("cancel", params, request_id)

    def _bambu_clear_error(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        return self._bambu_control("clear-error", params, request_id)

    # -- Python / JavaScript generators --------------------------------

    def _xml_generator(self, kind: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {"source", "output"}
        if kind == "sdf":
            allowed |= {"gz_check", "strict"}
        _validate_keys(params, allowed, required={"source"})
        source = self._input(params["source"], suffixes={".py"})
        output_name = _safe_filename(params.get("output", f"output.{kind}"), "output", suffixes={f".{kind}"})
        output = self.artifacts.output_path(request_id, output_name, suffixes={f".{kind}"})
        command = [sys.executable, str(self._script(kind, kind, "__main__.py")), str(source), "--output", str(output)]
        if kind == "sdf":
            command.extend(["--gz-check", _choice(params.get("gz_check"), "gz_check", {"auto", "required", "never"}, default="auto")])
            if _boolean(params.get("strict"), "strict"):
                command.append("--strict")
        return self._generator_run(kind, "generate", request_id, command)

    def _sdf_gz_check(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, {"input"}, required={"input"})
        source = self._input(params["input"], suffixes={".sdf"})
        return self._run("sdf", "gz-check", request_id, ["gz", "sdf", "--check", str(source)])

    def _implicit_export(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {"input", "output", "format", "resolution", "max_cells", "parameters"}
        _validate_keys(params, allowed, required={"input"})
        source = self._input(params["input"], suffixes={".implicit.js", ".implicit.mjs"})
        export_format = _choice(params.get("format"), "format", {"stl", "3mf", "glb"}, default="stl")
        output_name = _safe_filename(params.get("output", f"output.{export_format}"), "output", suffixes={f".{export_format}"})
        output = self.artifacts.output_path(request_id, output_name, suffixes={f".{export_format}"})
        command = [
            "node", str(self._script("implicit-cad", "export.mjs")), "--input", str(source), "--output", str(output),
            "--format", export_format, "--json",
        ]
        if "resolution" in params:
            command.extend(["--resolution", str(_integer(params["resolution"], "resolution", minimum=8, maximum=512))])
        if "max_cells" in params:
            command.extend(["--max-cells", str(_integer(params["max_cells"], "max_cells", minimum=1000, maximum=8_000_000))])
        if "parameters" in params:
            if not isinstance(params["parameters"], dict):
                raise CapabilityRequestError("parameters must be an object")
            encoded = json.dumps(params["parameters"], separators=(",", ":"))
            if len(encoded) > 32_000:
                raise CapabilityRequestError("parameters JSON is too large")
            command.extend(["--params", encoded])
        return self._generator_run("implicit-cad", "export", request_id, command)

    def _implicit_snapshot(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {"input", "output", "mode", "camera", "width", "height", "parameters"}
        _validate_keys(params, allowed, required={"input"})
        source = self._input(params["input"], suffixes={".implicit.js", ".implicit.mjs"})
        mode = _choice(params.get("mode"), "mode", {"view", "orbit", "animate"}, default="view")
        default_suffix = ".gif" if mode in {"orbit", "animate"} else ".png"
        output_name = _safe_filename(params.get("output", f"snapshot{default_suffix}"), "output", suffixes={".png", ".gif"})
        output = self.artifacts.output_path(request_id, output_name, suffixes={".png", ".gif"})
        command = ["node", str(self._script("implicit-cad", "snapshot.mjs")), "--input", str(source), "--output", str(output), "--mode", mode, "--json"]
        if params.get("camera"):
            command.extend(["--camera", _text(params["camera"], "camera", max_length=120)])
        for key in ("width", "height"):
            if key in params:
                command.extend([f"--{key}", str(_integer(params[key], key, minimum=64, maximum=4096))])
        if "parameters" in params:
            if not isinstance(params["parameters"], dict):
                raise CapabilityRequestError("parameters must be an object")
            command.extend(["--params", json.dumps(params["parameters"], separators=(",", ":"))])
        return self._generator_run("implicit-cad", "snapshot", request_id, command)

    # -- CAD STEP / inspect / snapshot ---------------------------------

    def _cad_step(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {"input", "kind", "output", "force", "mesh_tolerance", "mesh_angular_tolerance"}
        _validate_keys(params, allowed, required={"input"})
        raw_input = str(params["input"])
        source = self._input(raw_input, suffixes={".step", ".stp", ".py"})
        is_generator = source.suffix.lower() == ".py"
        output_name = _safe_filename(params.get("output", "output.step"), "output", suffixes={".step", ".stp"})
        output = self.artifacts.output_path(request_id, output_name, suffixes={".step", ".stp"})
        step_cli = str(self._script("cad", "step", "__main__.py"))
        if is_generator:
            command = [sys.executable, step_cli, str(source), "--output", str(output)]
        else:
            # Process a request-scoped copy so hidden viewer artifacts never
            # mutate the user's source directory.
            shutil.copy2(source, output)
            kind = _choice(params.get("kind"), "kind", {"part", "assembly"}, default="part")
            command = [sys.executable, step_cli, str(output), "--kind", kind]
        if _boolean(params.get("force"), "force"):
            command.append("--force")
        for key, flag in (("mesh_tolerance", "--mesh-tolerance"), ("mesh_angular_tolerance", "--mesh-angular-tolerance")):
            if key in params:
                command.extend([flag, str(_number(params[key], key, minimum=0.00001, maximum=10))])
        if is_generator:
            return self._generator_run("cad", "step", request_id, command)
        return self._run("cad", "step", request_id, command)

    def _cad_export(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(
            params,
            {"input", "format", "output", "kind", "force", "mesh_tolerance", "mesh_angular_tolerance"},
            required={"input", "format"},
        )
        source = self._input(params["input"], suffixes={".step", ".stp"})
        export_format = _choice(params["format"], "format", {"stl", "3mf", "glb"})
        output_name = _safe_filename(
            params.get("output", f"output.{export_format}"), "output", suffixes={f".{export_format}"}
        )
        request_dir = self.artifacts.request_dir(request_id)
        local_source = self.artifacts.output_path(request_id, source.name, suffixes={".step", ".stp"})
        if not local_source.exists():
            shutil.copy2(source, local_source)
        flag = {"stl": "--stl", "3mf": "--3mf", "glb": "--glb"}[export_format]
        command = [
            sys.executable, str(self._script("cad", "step", "__main__.py")), str(local_source),
            "--kind", _choice(params.get("kind"), "kind", {"part", "assembly"}, default="part"),
            flag, output_name,
        ]
        if _boolean(params.get("force"), "force"):
            command.append("--force")
        for key, option in (("mesh_tolerance", "--mesh-tolerance"), ("mesh_angular_tolerance", "--mesh-angular-tolerance")):
            if key in params:
                command.extend([option, str(_number(params[key], key, minimum=0.00001, maximum=10))])
        return self._run("cad", "export", request_id, command, cwd=request_dir)

    @staticmethod
    def _selector(value: object, name: str) -> str:
        return _text(value, name, max_length=120, pattern=r"#[A-Za-z0-9_.:-]+")

    def _cad_inspect(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {
            "operation", "input", "right", "selectors", "detail", "facts", "positioning", "planes", "topology",
            "from_selector", "to_selector", "selector", "moving", "target", "axis", "mode", "offset",
        }
        _validate_keys(params, allowed, required={"input"})
        operation = _choice(params.get("operation"), "operation", {"refs", "diff", "frame", "measure", "align"}, default="refs")
        entry = self._input(params["input"], suffixes={".step", ".stp"})
        command = [sys.executable, str(self._script("cad", "inspect", "__main__.py")), operation]
        if operation == "refs":
            command.append(str(entry))
            for selector in _string_list(params.get("selectors"), "selectors", maximum=100):
                command.append(self._selector(selector, "selector"))
            for key, flag in (("detail", "--detail"), ("facts", "--facts"), ("positioning", "--positioning"), ("planes", "--planes"), ("topology", "--topology")):
                if _boolean(params.get(key), key):
                    command.append(flag)
        elif operation == "diff":
            if "right" not in params:
                raise CapabilityRequestError("diff requires right")
            right = self._input(params["right"], suffixes={".step", ".stp"})
            command.extend([str(entry), str(right)])
            if _boolean(params.get("planes"), "planes"):
                command.append("--planes")
        elif operation == "frame":
            command.append(str(entry))
            if params.get("selector"):
                command.append(self._selector(params["selector"], "selector"))
        elif operation == "measure":
            if not params.get("from_selector") or not params.get("to_selector"):
                raise CapabilityRequestError("measure requires from_selector and to_selector")
            command.extend([str(entry), "--from", self._selector(params["from_selector"], "from_selector"), "--to", self._selector(params["to_selector"], "to_selector")])
            if params.get("axis"):
                command.extend(["--axis", _choice(params["axis"], "axis", {"x", "y", "z"})])
        else:
            if not params.get("moving") or not params.get("target"):
                raise CapabilityRequestError("align requires moving and target")
            command.extend([str(entry), "--moving", self._selector(params["moving"], "moving"), "--target", self._selector(params["target"], "target")])
            command.extend(["--mode", _choice(params.get("mode"), "mode", {"flush", "center"}, default="flush")])
            if params.get("axis"):
                command.extend(["--axis", _choice(params["axis"], "axis", {"x", "y", "z"})])
            if "offset" in params:
                command.extend(["--offset", str(_number(params["offset"], "offset", minimum=-1_000_000, maximum=1_000_000))])
        command.extend(["--format", "json", "--quiet"])
        return self._run("cad", "inspect", request_id, command)

    def _cad_snapshot(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {"input", "output", "mode", "camera", "width", "height", "size_profile", "focus", "hide"}
        _validate_keys(params, allowed, required={"input"})
        source = self._input(params["input"], suffixes={".step", ".stp"})
        local_source = self.artifacts.output_path(request_id, source.name, suffixes={".step", ".stp"})
        if not local_source.exists():
            shutil.copy2(source, local_source)
        mode = _choice(params.get("mode"), "mode", {"view", "orbit", "section", "list"}, default="view")
        suffixes = {".png", ".gif"}
        output_name = _safe_filename(params.get("output", "snapshot.png"), "output", suffixes=suffixes)
        output = self.artifacts.output_path(request_id, output_name, suffixes=suffixes)
        command = [sys.executable, str(self._script("cad", "snapshot", "__main__.py")), "--input", str(local_source), "--output", str(output), "--mode", mode, "--json"]
        if params.get("camera"):
            command.extend(["--camera", _text(params["camera"], "camera", max_length=120)])
        if params.get("size_profile"):
            command.extend(["--size-profile", _text(params["size_profile"], "size_profile", max_length=64, pattern=r"[A-Za-z0-9_-]+")])
        for key in ("width", "height"):
            if key in params:
                command.extend([f"--{key}", str(_integer(params[key], key, minimum=64, maximum=4096))])
        focus = _string_list(params.get("focus"), "focus", maximum=50)
        hide = _string_list(params.get("hide"), "hide", maximum=50)
        if focus and hide:
            raise CapabilityRequestError("focus and hide cannot be combined")
        for key, values in (("focus", focus), ("hide", hide)):
            for selector in values:
                command.extend([f"--{key}", self._selector(selector, key)])
        return self._run("cad", "snapshot", request_id, command, timeout=300)

    def _dxf_generate(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, {"source", "output", "verbose"}, required={"source"})
        source = self._input(params["source"], suffixes={".py"})
        output_name = _safe_filename(params.get("output", "output.dxf"), "output", suffixes={".dxf"})
        output = self.artifacts.output_path(request_id, output_name, suffixes={".dxf"})
        command = [sys.executable, str(self._script("dxf", "dxf", "__main__.py")), str(source), "--output", str(output)]
        if _boolean(params.get("verbose"), "verbose"):
            command.append("--verbose")
        return self._generator_run("dxf", "generate", request_id, command)

    def _dxf_validate(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, {"input"}, required={"input"})
        source = self._input(params["input"], suffixes={".dxf"})
        return self._base(
            "dxf", "validate", request_id, status="blocked", data={"input": str(source)},
            blocked_reasons=[
                "The vendored DXF package validates generated targets but does not expose a standalone DXF validator CLI."
            ],
        )

    # -- CAD Viewer -----------------------------------------------------

    @staticmethod
    def _viewer_url(port: int) -> str:
        return f"http://127.0.0.1:{port}/__cad/server"

    def _viewer_status(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, {"port"})
        port = _integer(params.get("port", 4178), "port", minimum=1024, maximum=65535)
        url = self._viewer_url(port)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "cad-agent-capability-runtime/1"})
            with urllib.request.urlopen(request, timeout=min(self.config.network_timeout_seconds, 5)) as response:
                payload = response.read(self.config.max_output_bytes + 1)
            if len(payload) > self.config.max_output_bytes:
                raise CapabilityRequestError("viewer status response exceeded the output limit")
            try:
                data: Any = json.loads(payload)
            except json.JSONDecodeError:
                data = payload.decode("utf-8", errors="replace")
            return self._base("cad-viewer", "status", request_id, status="succeeded", data={"running": True, "url": f"http://127.0.0.1:{port}/", "server": data})
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return self._base("cad-viewer", "status", request_id, status="succeeded", data={"running": False, "url": f"http://127.0.0.1:{port}/"})

    def _viewer_start(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, {"port", "directory", "shutdown_after"})
        port = _integer(params.get("port", 4178), "port", minimum=1024, maximum=65535)
        existing = self._viewer_status({"port": port}, request_id)
        if isinstance(existing.get("data"), dict) and existing["data"].get("running"):
            existing["action"] = "start"
            existing["checks"] = [{"name": "already_running", "passed": True}]
            return existing
        directory = self.workspace_root
        if params.get("directory"):
            directory = self.artifacts.workspace_path(params["directory"], must_exist=True, file_only=False)
            if not directory.is_dir():
                raise CapabilityRequestError("directory must be an existing workspace directory")
        lifetime = _text(params.get("shutdown_after", "12h"), "shutdown_after", max_length=16, pattern=r"(?:[1-9][0-9]{0,4})(?:ms|s|m|h)")
        server = self._script("cad-viewer", "viewer", "backend", "server.mjs")
        command = ["node", str(server), "--host", "127.0.0.1", "--port", str(port), "--dir", str(directory), "--shutdown-after", lifetime]
        preview = self._preview(command, ())
        if shutil.which("node") is None:
            return self._base("cad-viewer", "start", request_id, status="blocked", command_preview=preview, blocked_reasons=["Node.js is not installed."])
        try:
            process = subprocess.Popen(
                command,
                cwd=str(directory),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return self._base("cad-viewer", "start", request_id, status="blocked", command_preview=preview, blocked_reasons=["CAD Viewer could not be started."])
        return self._base(
            "cad-viewer", "start", request_id, status="succeeded", command_preview=preview,
            data={"pid": process.pid, "url": f"http://127.0.0.1:{port}/", "host": "127.0.0.1", "shutdown_after": lifetime},
            checks=[{"name": "loopback_only", "passed": True}],
        )

    def _viewer_review(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, {"input", "port"}, required={"input"})
        accepted = {
            ".step", ".stp", ".glb", ".stl", ".3mf", ".gcode", ".dxf", ".urdf", ".srdf", ".sdf",
            ".implicit.js", ".implicit.mjs",
        }
        source = self._input(params["input"], suffixes=accepted)
        port = _integer(params.get("port", 4178), "port", minimum=1024, maximum=65535)
        status = self._viewer_status({"port": port}, request_id)
        running = bool(isinstance(status.get("data"), dict) and status["data"].get("running"))
        if not running:
            started = self._viewer_start(
                {"port": port, "directory": str(self.workspace_root), "shutdown_after": "12h"}, request_id
            )
            running = started.get("status") == "succeeded"
        relative = source.relative_to(self.workspace_root).as_posix()
        from urllib.parse import quote

        url = f"http://127.0.0.1:{port}/?file={quote(relative, safe='/')}"
        return self._base(
            "cad-viewer", "review", request_id, status="succeeded" if running else "blocked",
            data={"running": running, "url": url, "file": str(source)},
            blocked_reasons=[] if running else ["CAD Viewer is unavailable and could not be started."],
            checks=[{"name": "workspace_confined", "passed": True}],
        )

    # -- SendCutSend official evidence ---------------------------------

    def _sendcutsend_fetch(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_keys(params, {"source"})
        selected = _choice(params.get("source"), "source", {*_OFFICIAL_SENDCUTSEND_SOURCES, "all"}, default="all")
        names = list(_OFFICIAL_SENDCUTSEND_SOURCES) if selected == "all" else [selected]
        fetched: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        for name in names:
            url = _OFFICIAL_SENDCUTSEND_SOURCES[name]
            request = urllib.request.Request(url, headers={"User-Agent": "cad-agent-capability-runtime/1"})
            try:
                with urllib.request.urlopen(request, timeout=self.config.network_timeout_seconds) as response:
                    content = response.read(self.config.max_fetch_bytes + 1)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                return self._base(
                    "sendcutsend", "fetch", request_id, status="blocked", data={"fetched": fetched}, files=files,
                    blocked_reasons=[f"Official SendCutSend source is unavailable: {name} ({type(exc).__name__})."],
                )
            if len(content) > self.config.max_fetch_bytes:
                return self._base(
                    "sendcutsend", "fetch", request_id, status="blocked", data={"fetched": fetched}, files=files,
                    blocked_reasons=[f"Official SendCutSend source exceeded the configured size limit: {name}."],
                )
            suffix = ".md" if name == "ordering-guide" else ".json"
            metadata = self.artifacts.write_bytes(request_id, f"sendcutsend-{name}{suffix}", content, suffixes={suffix})
            files.append(metadata)
            source_meta: Any = None
            if suffix == ".json":
                try:
                    parsed = json.loads(content)
                    source_meta = parsed.get("_meta") if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    source_meta = None
            fetched.append({
                "source": name,
                "url": url,
                "accessed_at": datetime.now(tz=UTC).isoformat(),
                "source_meta": source_meta,
                "sha256": metadata["sha256"],
                "size_bytes": metadata["size_bytes"],
            })
        return self._base(
            "sendcutsend", "fetch", request_id, status="succeeded", data={"fetched": fetched}, files=files,
            checks=[{"name": "official_origins_only", "passed": True}],
        )

    def _sendcutsend_preflight(self, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        allowed = {
            "input", "process", "material_sku", "thickness_mm", "quantity",
            "services", "finish", "hardware",
        }
        _validate_keys(params, allowed)
        fetched = self._sendcutsend_fetch({"source": "all"}, request_id)
        fetched["action"] = "preflight"
        if fetched["status"] != "succeeded":
            return fetched
        if not params.get("input"):
            fetched["status"] = "blocked"
            fetched["blocked_reasons"] = [
                "A readiness verdict requires the exact DXF, STEP, or STP upload candidate in input."
            ]
            return fetched

        source = self._input(params["input"], suffixes={".dxf", ".step", ".stp"})
        context: dict[str, Any] = {}
        for key in ("process", "material_sku", "finish", "hardware"):
            if params.get(key):
                context[key] = _text(params[key], key, max_length=160)
        if "thickness_mm" in params:
            context["thickness_mm"] = _number(params["thickness_mm"], "thickness_mm", minimum=0.001, maximum=10_000)
        if "quantity" in params:
            context["quantity"] = _integer(params["quantity"], "quantity", minimum=1, maximum=1_000_000)
        context["services"] = _string_list(params.get("services"), "services")

        checks: list[dict[str, Any]] = [
            {
                "name": "official_sources",
                "status": "pass",
                "message": "Current ordering guide, catalog, and engineering specs were fetched.",
                "source": "SendCutSend official evidence",
            }
        ]
        facts: dict[str, Any] = {
            "file": source.name,
            "sha256": sha256_file(source),
            "size_bytes": source.stat().st_size,
            "order_context": context,
        }

        if source.suffix.lower() == ".dxf":
            try:
                import ezdxf
                from ezdxf import bbox as ezdxf_bbox

                document = ezdxf.readfile(source)
                modelspace = document.modelspace()
                entities = list(modelspace)
                counts: dict[str, int] = {}
                for entity in entities:
                    counts[entity.dxftype()] = counts.get(entity.dxftype(), 0) + 1
                units_code = int(document.header.get("$INSUNITS", 0) or 0)
                unsupported = sorted(name for name in ("TEXT", "MTEXT", "DIMENSION", "IMAGE") if counts.get(name))
                open_polylines = sum(
                    1 for entity in entities
                    if entity.dxftype() in {"LWPOLYLINE", "POLYLINE"} and not bool(getattr(entity, "closed", False))
                )
                try:
                    extents = ezdxf_bbox.extents(entities, fast=True)
                    bounds = {
                        "min": list(extents.extmin.xyz),
                        "max": list(extents.extmax.xyz),
                    } if extents.has_data else None
                except Exception:
                    bounds = None
                facts["dxf"] = {
                    "insunits": units_code,
                    "entity_counts": counts,
                    "open_polylines": open_polylines,
                    "unsupported_entities": unsupported,
                    "bounds": bounds,
                    "layers": sorted({entity.dxf.layer for entity in entities if hasattr(entity.dxf, "layer")}),
                }
                checks.append({
                    "name": "dxf_units",
                    "status": "pass" if units_code in {1, 4} else "fail",
                    "message": f"$INSUNITS={units_code}; expected 1 (inch) or 4 (mm).",
                    "source": "SendCutSend ordering guide + direct file inspection",
                })
                checks.append({
                    "name": "unsupported_dxf_entities",
                    "status": "fail" if unsupported else "pass",
                    "message": "Unsupported annotations: " + ", ".join(unsupported) if unsupported else "No text, dimensions, or image entities found.",
                    "source": "SendCutSend ordering guide + direct file inspection",
                })
                checks.append({
                    "name": "open_contours",
                    "status": "fail" if open_polylines else "pass",
                    "message": f"Found {open_polylines} open polyline contour(s).",
                    "source": "Direct file inspection",
                })
            except Exception as exc:
                checks.append({
                    "name": "dxf_parse",
                    "status": "fail",
                    "message": f"DXF inspection failed: {type(exc).__name__}.",
                    "source": "Direct file inspection",
                })
        else:
            inspected = self._cad_inspect(
                {"input": str(source), "operation": "refs", "facts": True, "planes": True, "positioning": True},
                request_id,
            )
            facts["step_inspection"] = inspected.get("data")
            checks.append({
                "name": "step_geometry",
                "status": "pass" if inspected.get("status") == "succeeded" else "unknown",
                "message": "STEP geometry inspection completed." if inspected.get("status") == "succeeded" else "STEP geometry inspection runtime is unavailable.",
                "source": "Direct file inspection",
            })

        sku = context.get("material_sku")
        if sku:
            matches: list[dict[str, Any]] = []

            def visit(value: Any) -> None:
                if len(matches) >= 20:
                    return
                if isinstance(value, dict):
                    candidate = value.get("sku") or value.get("SKU")
                    if candidate is not None and str(candidate).casefold() == str(sku).casefold():
                        matches.append({
                            key: item for key, item in value.items()
                            if key in {"sku", "SKU", "name", "material", "thickness", "thickness_mm", "process", "services", "in_stock"}
                        })
                    for item in value.values():
                        visit(item)
                elif isinstance(value, list):
                    for item in value:
                        visit(item)

            for metadata in fetched.get("files", []):
                path = Path(str(metadata.get("path", "")))
                if path.suffix.lower() == ".json" and path.is_file():
                    try:
                        visit(json.loads(path.read_text(encoding="utf-8")))
                    except (OSError, json.JSONDecodeError):
                        pass
            facts["material_sku_matches"] = matches
            checks.append({
                "name": "material_sku",
                "status": "pass" if matches else "unknown",
                "message": f"Found {len(matches)} exact current source record(s) for SKU {sku}." if matches else f"No exact current source record was found for SKU {sku}.",
                "source": "SendCutSend catalog/specs exact SKU lookup",
            })
        else:
            checks.append({
                "name": "material_sku",
                "status": "unknown",
                "message": "Material SKU is required for material-, thickness-, and service-specific readiness checks.",
                "source": "Order context",
            })

        statuses = {check["status"] for check in checks}
        verdict = "fail" if "fail" in statuses else "unknown"
        report = {
            "verdict": verdict,
            "ready": False,
            "facts": facts,
            "checks": checks,
            "limitations": [
                "A ready verdict is withheld until every selected service has an exact source requirement and measured file fact.",
                "Bend-local flange/contact checks and STEP bend-radius/tool-access checks require dedicated geometry measurements.",
            ],
            "sources": fetched.get("data", {}).get("fetched", []),
        }
        report_metadata = self.artifacts.write_bytes(
            request_id,
            "sendcutsend-preflight.json",
            json.dumps(report, indent=2).encode("utf-8"),
            suffixes={".json"},
        )
        files = [*fetched.get("files", []), report_metadata]
        return self._base(
            "sendcutsend",
            "preflight",
            request_id,
            status="succeeded",
            data=report,
            files=files,
            checks=checks,
        )


def execute_capability(
    capability: str,
    action: str,
    params: Mapping[str, Any] | object | None = None,
    request_id: str | None = None,
    *,
    runtime: CapabilityRuntime | None = None,
    config: RuntimeConfig | None = None,
) -> dict[str, Any]:
    """Convenience wrapper used by HTTP/API integration code."""

    active_runtime = runtime or CapabilityRuntime(config=config)
    return active_runtime.execute(capability, action, params, request_id=request_id)
