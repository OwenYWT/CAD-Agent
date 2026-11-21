"""Execute authenticated CAD Skills actions in their owner workspace."""
from pathlib import Path
import re
from typing import Any

from app.capabilities.runtime import CapabilityRuntime, RuntimeConfig, uses_native_worker as uses_native_worker
from app.config import settings


close_viewers = CapabilityRuntime.close_viewers


def owner_root(owner_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", owner_id):
        raise ValueError("Invalid CAD Skills workspace owner")
    root = Path(settings.file_storage_dir).expanduser().resolve()
    owner = (root / "capability_users" / owner_id).resolve()
    owner.mkdir(parents=True, exist_ok=True)
    return owner


def _runtime(owner_id: str, execution_backend=None) -> CapabilityRuntime:
    owner = owner_root(owner_id)
    return CapabilityRuntime(
        RuntimeConfig(
            workspace_root=owner,
            artifact_root=owner / "runs",
            isolated_executor=tuple(settings.cadskills_isolated_executor) or None,
            allow_bambu_lan=settings.cadskills_enable_bambu_lan,
            tenant_id=owner_id,
            project_id=f"capability-{owner_id}",
        ),
        execution_backend=execution_backend,
    )


def _public_result(result: dict[str, Any], owner_id: str) -> dict[str, Any]:
    owner = owner_root(owner_id)
    storage = Path(settings.file_storage_dir).expanduser().resolve()
    repo = Path(__file__).resolve().parents[3]

    def clean_text(value: str) -> str:
        return (
            value.replace(str(owner), "$WORKSPACE")
            .replace(str(storage), "$WORKSPACE")
            .replace(str(repo), "$APP")
        )

    def clean_value(value: Any) -> Any:
        if isinstance(value, str):
            return clean_text(value)
        if isinstance(value, list):
            return [clean_value(item) for item in value]
        if isinstance(value, dict):
            return {key: clean_value(item) for key, item in value.items()}
        return value

    public_files: list[dict[str, Any]] = []
    for item in result.get("files") or []:
        public = {key: value for key, value in item.items() if key != "path"}
        relative = str(public.get("relative_path") or "")
        parts = Path(relative).parts
        if len(parts) >= 2:
            request_id, filename = parts[0], parts[-1]
            public["url"] = f"/api/capability-artifacts/runs/{request_id}/{filename}"
            public["input"] = f"runs/{relative}"
        public_files.append(public)
    result["files"] = public_files
    result["command_preview"] = [clean_text(str(part)) for part in result.get("command_preview") or []]
    for field in ("data", "blocked_reasons", "error"):
        if field in result:
            result[field] = clean_value(result[field])
    return result


async def perform_action(owner_id: str, capability_id: str, action_id: str, params: dict,
                         request_id: str | None, execution_backend=None) -> dict:
    owner = owner_root(owner_id)
    if settings.durable_control_plane_enabled:
        from app.storage.capability_artifacts import hydrate_values

        await hydrate_values(params, owner)
    runtime = _runtime(owner_id, execution_backend)
    result = await runtime.execute_async(
        capability_id,
        action_id,
        params,
        request_id,
    )
    if settings.durable_control_plane_enabled:
        from app.storage.capability_artifacts import commit

        for item in result.get("files") or []:
            path_value = item.get("path")
            if not path_value:
                continue
            path = Path(path_value).resolve()
            await commit(
                scope="runs",
                request_id=result["request_id"],
                filename=path.name,
                path=path,
                content_type=item.get("media_type"),
            )
    return _public_result(result, owner_id)
