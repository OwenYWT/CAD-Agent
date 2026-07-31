"""Compatibility and fail-closed checks for the durable write boundary."""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.batch import AsyncGenerateRequest, BatchItem
from app.api.websocket import _durable_identity_payload
from app.config import settings
from app.models.schemas import ExecuteRequest, GenerateRequest, ModifyRequest


def _identity():
    return {
        "project_id": uuid4(),
        "branch_id": uuid4(),
        "expected_base_revision_id": uuid4(),
        "idempotency_key": f"request-{uuid4()}",
    }


def test_legacy_request_contract_is_unchanged_before_atomic_cutover(monkeypatch):
    monkeypatch.setattr(settings, "durable_api_cutover_enabled", False)

    GenerateRequest(prompt="创建支架")
    ModifyRequest(code="result = box(1, 1, 1)", prompt="加厚")
    ExecuteRequest(code="result = box(2, 2, 2)")
    BatchItem(prompt="创建面板")
    AsyncGenerateRequest(prompt="创建外壳")
    assert _durable_identity_payload({"type": "execute_code"}) == {}


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
def test_cutover_rejects_incomplete_optimistic_concurrency_identity(
    monkeypatch,
    model,
    payload,
):
    monkeypatch.setattr(settings, "durable_api_cutover_enabled", True)
    with pytest.raises(ValidationError, match="expected_base_revision_id"):
        model.model_validate(payload)


def test_cutover_accepts_one_complete_identity_across_http_and_websocket(
    monkeypatch,
):
    monkeypatch.setattr(settings, "durable_api_cutover_enabled", True)
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


def test_current_and_stale_identities_remain_distinct_across_all_parsers(
    monkeypatch,
):
    monkeypatch.setattr(settings, "durable_api_cutover_enabled", True)
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


def test_cutover_cannot_be_enabled_without_the_control_plane(monkeypatch):
    monkeypatch.setattr(settings, "durable_control_plane_enabled", False)
    monkeypatch.setattr(settings, "durable_api_cutover_enabled", True)
    assert settings.durable_control_plane_config_problems() == [
        "DURABLE_API_CUTOVER_ENABLED requires DURABLE_CONTROL_PLANE_ENABLED."
    ]
