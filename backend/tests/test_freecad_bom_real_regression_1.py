from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.code_gen import CodeGenerator
from app.execution.capability_adapter import CapabilityExecutionAdapter
from app.execution.contracts import ExecutionStatus
from app.freecad.bom_contracts import FreeCADBOMRequestV1
from app.sandbox.executor import CadQueryExecutor
from tests.native_runtime import native_backend, native_executor


# Regression: FUSION-004 — the durable Assembly workflow could not reach a
# native BOM because its manifest contract and query were broken.  This test
# additionally proves that the same real CadQuery STEP artifacts accepted by
# the workflow can be consumed by FreeCAD 1.1.3 Assembly::BomObject.


async def _cadquery_step(executor: CadQueryExecutor, source: str) -> tuple[Path, Path]:
    outcome = await executor.execute(source, mode="3d", timeout_s=180)
    assert outcome.success, outcome.error_message
    return outcome.files["result.step"], outcome.work_dir


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_FREECAD") != "1",
    reason="set RUN_REAL_FREECAD=1 to exercise native FreeCAD Assembly BOM",
)
async def test_real_cadquery_assembly_steps_generate_native_freecad_bom() -> None:
    executor = native_executor()
    backend = native_backend()
    adapter = CapabilityExecutionAdapter(backend)
    work_dirs: list[Path] = []
    try:
        base_source = "result = cq.Workplane('XY').box(30, 20, 4)\n"
        base, base_dir = await _cadquery_step(
            executor,
            base_source,
        )
        work_dirs.append(base_dir)
        lid_source = (
            "result = cq.Workplane('XY').box("
            "30, 20, 2, centered=(True, True, False))\n"
        )
        lid, lid_dir = await _cadquery_step(
            executor,
            lid_source,
        )
        work_dirs.append(lid_dir)
        assembly_source = await CodeGenerator().generate_assembly_combiner([
            {
                "name": "part_01",
                "label": "base",
                "code": base_source,
                "position": [0, 0, 0],
                "color": "steelblue",
            },
            {
                "name": "part_02",
                "label": "lid",
                "code": lid_source,
                "position": [0, 0, 4],
                "color": "orange",
            },
        ])
        assembly, assembly_dir = await _cadquery_step(
            executor,
            assembly_source,
        )
        work_dirs.append(assembly_dir)

        snapshot = backend.runtime_snapshot()
        request = FreeCADBOMRequestV1(
            candidate_build_id=uuid4(),
            base_revision_id=uuid4(),
            plan_hash="a" * 64,
            runtime_image_digest=snapshot.image_digest,
            combine_step_key="combine",
            components=(
                {
                    "step_key": "part-01",
                    "label": "base",
                    "position_mm": (0, 0, 0),
                    "artifact_id": "component:part-01",
                },
                {
                    "step_key": "part-02",
                    "label": "lid",
                    "position_mm": (0, 0, 4),
                    "artifact_id": "component:part-02",
                },
            ),
            property_columns=(),
        )
        outcome = await adapter.execute(
            capability="freecad",
            operation="bom",
            request_id="native-bom-regression",
            params={"request": request.model_dump(mode="json")},
            inputs={
                "assembly": assembly,
                "component:part-01": base,
                "component:part-02": lid,
            },
            artifact_media_type="application/json",
            mode="analysis",
            timeout_seconds=180,
            output_bytes=128 * 1024 * 1024,
            declared_outputs={
                "bom-json": "application/json",
                "bom-csv": "text/csv; charset=utf-8",
            },
        )
        assert outcome.execution.result.status is ExecutionStatus.SUCCEEDED, (
            json.dumps(
                outcome.execution.result.error.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
            if outcome.execution.result.error
            else "missing execution error"
        )
        work_dirs.append(outcome.execution.work_dir)
        document = json.loads(
            outcome.execution.files["bom-json"].read_text(encoding="utf-8")
        )
        assert document["generator"]["native_type"] == "Assembly::BomObject"
        assert [row["name"] for row in document["rows"]] == ["base", "lid"]
        assert sum(row["quantity"] for row in document["rows"]) == 2
    finally:
        for work_dir in work_dirs:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
