from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from eval_config import default_backend_dir


class DirectAlgorithmClient:
    """In-process adapter for the current CAD-Agent algorithm.

    Default mode uses the durable workflow submission path used by the backend
    APIs. Passing ``orchestrator=...`` keeps the legacy test seam for unit tests
    and for explicitly comparing the old Orchestrator-only flow.
    """

    def __init__(
        self,
        orchestrator: Any | None = None,
        backend_dir: str | Path | None = None,
        timeout_s: float | None = None,
        eval_user_id: str = "eval-algorithms-local",
    ):
        self.backend_dir = Path(backend_dir or default_backend_dir()).resolve()
        self.timeout_s = timeout_s
        self.eval_user_id = eval_user_id
        self._legacy_orchestrator = orchestrator

    def get_json(self, path: str) -> dict[str, Any]:
        if path == "/health":
            return {"status": "ok", "runner": "direct-durable"}
        if path == "/ready":
            self._prepare_backend_imports()
            from app.config import settings

            return {
                "status": "ok" if settings.has_llm_credentials else "degraded",
                "runner": "direct-durable",
                "backend_dir": str(self.backend_dir),
                "llm_provider": settings.llm_provider,
                "sandbox_runtime": settings.sandbox_runtime,
                "sandbox_image": settings.sandbox_image,
            }
        return {"success": False, "error": {"type": "ValueError", "message": f"Unsupported direct GET route: {path}"}}

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._legacy_orchestrator is not None:
                return self._post_legacy(path, payload)
            if path == "/api/generate":
                return self._response_to_dict(self._run(self._submit_durable("generate", payload)))
            if path == "/api/execute":
                return self._response_to_dict(self._run(self._submit_durable("execute", payload)))
            raise ValueError(f"Unsupported direct POST route: {path}")
        except Exception as exc:
            return {"success": False, "error": {"type": type(exc).__name__, "message": str(exc)}}

    def _post_legacy(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/api/generate":
            coro = self._legacy_orchestrator.generate(payload["prompt"], payload.get("output_formats") or ["step", "stl"])
        elif path == "/api/execute":
            coro = self._legacy_orchestrator.execute_code(payload["code"], payload.get("output_formats") or ["step", "stl"])
        else:
            raise ValueError(f"Unsupported direct POST route: {path}")
        return self._response_to_dict(self._run(coro))

    async def _submit_durable(self, operation: str, payload: dict[str, Any]):
        self._prepare_backend_imports()
        from app.domain.identity import user_principal
        from app.principal_context import bind_principal
        from app.services.durable_submission import (
            ensure_workspace_identity,
            submit_durable_workflow,
            wait_for_compatibility_response,
        )
        from app.config import settings

        principal = user_principal(self.eval_user_id)
        bind_principal(principal)
        sample_label = str(payload.get("sample_id") or uuid4().hex[:12])
        session_id = str(payload.get("session_id") or f"eval-{operation}-{sample_label}-{uuid4().hex[:8]}")
        panel_id = str(payload.get("panel_id") or f"panel-{operation}-{sample_label}-{uuid4().hex[:8]}")
        objective = str(payload.get("prompt") or "Execute CAD reference code")
        workspace = await ensure_workspace_identity(
            principal,
            session_id=session_id,
            panel_id=panel_id,
            title=objective[:80],
            user_id=self.eval_user_id,
        )
        submission = await submit_durable_workflow(
            principal,
            project_id=workspace.project_id,
            branch_id=workspace.branch_id,
            expected_base_revision_id=workspace.head_revision_id,
            idempotency_key=str(payload.get("idempotency_key") or uuid4()),
            operation=operation,
            objective=objective,
            output_formats=payload.get("output_formats") or ["step", "stl"],
            code=payload.get("code") if operation == "execute" else None,
            manufacturing_profile=payload.get("manufacturing_profile"),
        )
        timeout_s = float(self.timeout_s or settings.generate_deadline_s)
        return await wait_for_compatibility_response(
            principal,
            submission,
            timeout_seconds=timeout_s,
        )

    def _run(self, coro):
        timeout_s = self.timeout_s

        async def with_timeout():
            if timeout_s and timeout_s > 0:
                return await asyncio.wait_for(coro, timeout=timeout_s)
            return await coro

        return asyncio.run(with_timeout())

    def _response_to_dict(self, response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        if hasattr(response, "model_dump"):
            try:
                return response.model_dump(mode="json")
            except TypeError:
                return response.model_dump()
        if hasattr(response, "dict"):
            return response.dict()
        raise TypeError(f"Unsupported response type: {type(response).__name__}")

    def _prepare_backend_imports(self) -> None:
        if str(self.backend_dir) not in sys.path:
            sys.path.insert(0, str(self.backend_dir))
        os.chdir(self.backend_dir)
