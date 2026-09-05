from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.operation_resolution import (
    OperationResolutionError,
    RevisionSourceInventory,
    TrustedBaseSource,
    resolve_browser_submission,
    resolve_rest_modify_submission,
)


def _source(kind: str, *, code: str | None = None) -> TrustedBaseSource:
    return TrustedBaseSource(
        kind=kind,
        source_id=uuid4(),
        sha256="a" * 64,
        code=code,
    )


def test_browser_modify_prefers_committed_fcstd_over_browser_code():
    resolution = resolve_browser_submission(
        requested_operation="modify",
        rule="explicit_ui_intent",
        panel_id="panel-1",
        base_revision_id=uuid4(),
        inventory=RevisionSourceInventory(
            fcstd=(_source("fcstd_artifact"),),
            cadquery=(_source("agent_generated_source", code="trusted"),),
        ),
        browser_code="untrusted",
    )

    assert resolution.operation == "modify"
    assert resolution.modeling_backend == "freecad"
    assert resolution.existing_code is None
    assert resolution.operation_context.base_source_kind == "fcstd_artifact"


def test_browser_legacy_message_uses_verified_persisted_cadquery_source():
    resolution = resolve_browser_submission(
        requested_operation=None,
        rule="legacy_editable_base_present",
        panel_id="panel-1",
        base_revision_id=uuid4(),
        inventory=RevisionSourceInventory(
            cadquery=(_source("agent_generated_source", code="trusted"),),
        ),
        browser_code=None,
    )

    assert resolution.operation == "modify"
    assert resolution.modeling_backend == "cadquery"
    assert resolution.existing_code == "trusted"


def test_browser_explicit_generate_rejects_overwriting_an_editable_panel():
    with pytest.raises(OperationResolutionError) as captured:
        resolve_browser_submission(
            requested_operation="generate",
            rule="explicit_ui_intent",
            panel_id="panel-1",
            base_revision_id=uuid4(),
            inventory=RevisionSourceInventory(
                fcstd=(_source("fcstd_artifact"),),
            ),
            browser_code=None,
        )

    assert captured.value.code == "new_conversation_required"


def test_browser_rejects_ambiguous_fcstd_without_falling_back():
    with pytest.raises(OperationResolutionError) as captured:
        resolve_browser_submission(
            requested_operation="modify",
            rule="explicit_modify_part",
            panel_id="panel-1",
            base_revision_id=uuid4(),
            inventory=RevisionSourceInventory(
                fcstd=(
                    _source("fcstd_artifact"),
                    _source("fcstd_artifact"),
                ),
            ),
            browser_code=None,
        )

    assert captured.value.code == "modify_base_fcstd_ambiguous"


def test_rest_auto_without_code_requires_one_fcstd():
    with pytest.raises(OperationResolutionError) as captured:
        resolve_rest_modify_submission(
            requested_backend="auto",
            base_revision_id=uuid4(),
            inventory=RevisionSourceInventory(),
            request_code=None,
        )

    assert captured.value.code == "modify_base_fcstd_missing"


def test_rest_cadquery_requires_code_and_freecad_forbids_it():
    with pytest.raises(OperationResolutionError) as missing:
        resolve_rest_modify_submission(
            requested_backend="cadquery",
            base_revision_id=uuid4(),
            inventory=RevisionSourceInventory(),
            request_code=None,
        )
    assert missing.value.code == "modify_code_required"

    with pytest.raises(OperationResolutionError) as forbidden:
        resolve_rest_modify_submission(
            requested_backend="freecad",
            base_revision_id=uuid4(),
            inventory=RevisionSourceInventory(
                fcstd=(_source("fcstd_artifact"),),
            ),
            request_code="result = box(1, 1, 1)",
        )
    assert forbidden.value.code == "modify_code_not_allowed"
