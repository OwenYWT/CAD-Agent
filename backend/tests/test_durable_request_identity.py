"""Compatibility and fail-closed checks for the durable write boundary."""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.api.batch import AsyncGenerateRequest, BatchItem
from app.api.websocket import _durable_identity_payload
from app.db import tenant_transaction
from app.domain.identity import local_anonymous_principal
from app.models.schemas import ExecuteRequest, GenerateRequest, ModifyRequest
from app.services.durable_submission import ensure_workspace_identity
from app.storage import history


def _identity():
    return {
        "project_id": uuid4(),
        "branch_id": uuid4(),
        "expected_base_revision_id": uuid4(),
        "idempotency_key": f"request-{uuid4()}",
    }


def test_durable_identity_is_always_required():
    for model, payload in (
        (GenerateRequest, {"prompt": "创建支架"}),
        (ModifyRequest, {
            "code": "result = box(1, 1, 1)",
            "prompt": "加厚",
        }),
        (ExecuteRequest, {"code": "result = box(2, 2, 2)"}),
        (BatchItem, {"prompt": "创建面板"}),
        (AsyncGenerateRequest, {"prompt": "创建外壳"}),
    ):
        with pytest.raises(ValidationError, match="durable MCAD writes"):
            model.model_validate(payload)
    with pytest.raises(ValidationError, match="durable MCAD writes"):
        _durable_identity_payload({"type": "execute_code"})


def test_rest_write_schema_exposes_non_nullable_required_identity():
    schema = ModifyRequest.model_json_schema()

    assert {
        "project_id",
        "branch_id",
        "expected_base_revision_id",
        "idempotency_key",
        "prompt",
    } <= set(schema["required"])
    for field in (
        "project_id",
        "branch_id",
        "expected_base_revision_id",
        "idempotency_key",
    ):
        assert "anyOf" not in schema["properties"][field]


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (GenerateRequest, {"prompt": "创建支架"}),
        (
            ModifyRequest,
            {"code": "result = box(1, 1, 1)", "prompt": "加厚"},
        ),
        (ExecuteRequest, {"code": "result = box(2, 2, 2)"}),
        (BatchItem, {"prompt": "创建面板"}),
        (AsyncGenerateRequest, {"prompt": "创建外壳"}),
    ],
)
def test_rejects_incomplete_optimistic_concurrency_identity(model, payload):
    with pytest.raises(ValidationError, match="expected_base_revision_id"):
        model.model_validate(payload)


def test_accepts_one_complete_identity_across_http_and_websocket():
    identity = _identity()

    request = ExecuteRequest.model_validate({
        "code": "result = box(2, 2, 2)",
        **identity,
    })
    websocket = _durable_identity_payload(identity)

    assert request.expected_base_revision_id == identity[
        "expected_base_revision_id"
    ]
    assert websocket == {
        key: str(value) if key != "idempotency_key" else value
        for key, value in identity.items()
    }


def test_modify_request_accepts_real_freecad_request_without_source_code():
    request = ModifyRequest.model_validate({
        **_identity(),
        "prompt": "将孔径修改为 8 mm",
        "modeling_backend": "freecad",
    })

    assert request.code is None
    assert request.modeling_backend == "freecad"


def test_modify_request_defaults_to_auto_for_legacy_compatibility():
    request = ModifyRequest.model_validate({
        **_identity(),
        "prompt": "加厚",
        "code": "result = box(1, 1, 1)",
    })

    assert request.modeling_backend == "auto"


def test_current_and_stale_identities_remain_distinct_across_all_parsers():
    project_id = uuid4()
    branch_id = uuid4()
    current_revision_id = uuid4()
    stale_revision_id = uuid4()
    current = {
        "project_id": project_id,
        "branch_id": branch_id,
        "expected_base_revision_id": current_revision_id,
        "idempotency_key": f"current-{uuid4()}",
    }
    stale = {
        **current,
        "expected_base_revision_id": stale_revision_id,
        "idempotency_key": f"stale-{uuid4()}",
    }
    parsers = [
        (GenerateRequest, {"prompt": "创建支架"}),
        (ModifyRequest, {
            "code": "result = box(1, 1, 1)",
            "prompt": "加厚",
        }),
        (ExecuteRequest, {"code": "result = box(2, 2, 2)"}),
        (BatchItem, {"prompt": "创建面板"}),
        (AsyncGenerateRequest, {"prompt": "创建外壳"}),
    ]
    for model, payload in parsers:
        current_request = model.model_validate({**payload, **current})
        stale_request = model.model_validate({**payload, **stale})
        assert current_request.expected_base_revision_id == current_revision_id
        assert stale_request.expected_base_revision_id == stale_revision_id
        assert current_request.idempotency_key != stale_request.idempotency_key

    assert _durable_identity_payload(current)[
        "expected_base_revision_id"
    ] == str(current_revision_id)
    assert _durable_identity_payload(stale)[
        "expected_base_revision_id"
    ] == str(stale_revision_id)


@pytest.mark.asyncio
async def test_ensure_workspace_identity_creates_session_and_branch(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr("app.config.settings.durable_control_plane_enabled", False)
    await history.close_db()

    principal = local_anonymous_principal()
    session_id = f"session-{uuid4()}"
    panel_id = f"panel-{uuid4()}"
    workspace = await ensure_workspace_identity(
        principal,
        session_id=session_id,
        panel_id=panel_id,
        title="Test Session",
        user_id=None,
    )

    assert workspace.project_id
    assert workspace.branch_id
    assert workspace.head_revision_id

    async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
        project_id = await connection.scalar(
            text(
                """
                SELECT project_id
                FROM workspace_sessions
                WHERE tenant_id=:tenant_id AND id=:session_id
                """
            ),
            {"tenant_id": principal.tenant_id, "session_id": session_id},
        )

    assert str(project_id) == str(workspace.project_id)
