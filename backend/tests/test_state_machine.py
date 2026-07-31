from types import SimpleNamespace

import pytest

from app.agent.failure_taxonomy import FixPath
from app.agent.state_machine import ExecutionPhase, ExecutionStateMachine


class FakeCodeGen:
    def __init__(self, fixed_codes: list[str] | None = None):
        self.fixed_codes = fixed_codes or []
        self.fix_error_calls = []
        self.fix_visual_calls = []

    async def fix_error(self, code, error, plan):
        self.fix_error_calls.append((code, error, plan))
        if self.fixed_codes:
            return self.fixed_codes.pop(0)
        return f"{code}\n# fixed"

    async def fix_visual_issues(self, code, issues, suggestions, on_step=None):
        self.fix_visual_calls.append((code, issues, suggestions, on_step))
        return f"{code}\n# visual fixed"


class FakeExecutor:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def execute(self, code, mode='3d'):
        self.calls.append((code, mode))
        return self.results.pop(0)


class FakeGeometryValidator:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def validate(self, stl_path, expected_dims=None):
        self.calls.append((stl_path, expected_dims))
        return self.result


class FakeRenderer:
    def __init__(self, render_paths):
        self.render_paths = render_paths
        self.calls = []

    def render_stl(self, stl_path, renders_dir):
        self.calls.append((stl_path, renders_dir))
        return self.render_paths


class FakeVisionValidator:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def validate(self, user_prompt, render_paths, code):
        self.calls.append((user_prompt, list(render_paths), code))
        return self.result


class FakeOrchestrator:
    def __init__(self, *, executor, code_gen, geometry_validator=None, renderer=None, vision_validator=None, stl_path=None):
        self.executor = executor
        self.code_gen = code_gen
        self.geometry_validator = geometry_validator
        self.renderer = renderer
        self.vision_validator = vision_validator
        self.stl_path = stl_path
        self.indeterminate_checks = []

    def _copy_output_files(self, work_dir, request_id, output_formats):
        return {fmt: f"/api/files/{request_id}/result.{fmt}" for fmt in output_formats}

    def _extract_params(self, code):
        return {}

    def _find_file_in_output(self, work_dir, ext):
        return None

    def _find_stl_in_output(self, work_dir):
        return self.stl_path

    def _add_indeterminate_vision_check(self, inspect_report, message):
        self.indeterminate_checks.append((inspect_report, message))


@pytest.mark.asyncio
async def test_state_machine_successful_3d_flow(tmp_path, monkeypatch):
    work_dir = tmp_path / 'success'
    work_dir.mkdir()
    stl_path = work_dir / 'result.stl'
    stl_path.write_text('solid mock')

    geometry_result = SimpleNamespace(
        passed=True,
        is_watertight=True,
        bounding_box={
            'x_min': 0,
            'x_max': 1,
            'y_min': 0,
            'y_max': 2,
            'z_min': 0,
            'z_max': 3,
        },
        volume=4.0,
        printable=True,
        fits_build_volume=True,
        min_wall_thickness=0.8,
        print_warnings=[],
        rules=[],
    )
    vision_result = SimpleNamespace(is_match=True, issues=[], suggestions=[])
    orchestrator = FakeOrchestrator(
        executor=FakeExecutor([SimpleNamespace(success=True, work_dir=work_dir, execution_time_ms=12)]),
        code_gen=FakeCodeGen(),
        geometry_validator=FakeGeometryValidator(geometry_result),
        renderer=FakeRenderer([work_dir / 'renders' / 'view.png']),
        vision_validator=FakeVisionValidator(vision_result),
        stl_path=stl_path,
    )

    monkeypatch.setattr('app.agent.state_machine.validate_code', lambda code: (True, None))
    monkeypatch.setattr('app.agent.state_machine.analyze_code', lambda code: [])
    monkeypatch.setattr(
        'app.agent.state_machine.build_inspect_report',
        lambda geo, **kwargs: {'source': kwargs['source'], 'available_exports': kwargs['available_exports']},
    )

    state_machine = ExecutionStateMachine(
        orchestrator=orchestrator,
        request_id='req-success',
        code='print("ok")',
        plan=SimpleNamespace(design_brief=SimpleNamespace(), dimensions=None),
        output_formats=['step', 'stl'],
    )

    response = await state_machine.run()

    assert response.success is True
    assert response.validation is not None
    assert response.inspect_report.source == 'geometry_validator'
    assert response.inspect_report.available_exports == ['step', 'stl']
    assert response.files['stl'].endswith('result.stl')
    assert [item.phase for item in state_machine.trace] == [
        ExecutionPhase.VALIDATE_CODE,
        ExecutionPhase.STATIC_ANALYSIS,
        ExecutionPhase.EXECUTE_CODE,
        ExecutionPhase.GEOMETRY_VALIDATE,
        ExecutionPhase.VISION_VALIDATE,
        ExecutionPhase.COMPLETE,
    ]


@pytest.mark.asyncio
async def test_state_machine_recovers_from_validation_failure(tmp_path, monkeypatch):
    work_dir = tmp_path / 'validation'
    work_dir.mkdir()
    validate_calls = {'count': 0}

    async def execute_once(code, mode='3d'):
        return SimpleNamespace(success=True, work_dir=work_dir, execution_time_ms=9)

    def validate_code(code):
        validate_calls['count'] += 1
        if validate_calls['count'] == 1:
            return False, 'bad import'
        return True, None

    orchestrator = FakeOrchestrator(
        executor=FakeExecutor([SimpleNamespace(success=True, work_dir=work_dir, execution_time_ms=9)]),
        code_gen=FakeCodeGen(['print("repaired")']),
    )
    orchestrator.executor.execute = execute_once

    monkeypatch.setattr('app.agent.state_machine.validate_code', validate_code)
    monkeypatch.setattr('app.agent.state_machine.analyze_code', lambda code: [])

    state_machine = ExecutionStateMachine(
        orchestrator=orchestrator,
        request_id='req-validation',
        code='import forbidden_module',
        plan=SimpleNamespace(design_brief=SimpleNamespace(), dimensions=None),
        output_formats=['step'],
    )

    response = await state_machine.run()

    assert response.success is True
    assert orchestrator.code_gen.fix_error_calls[0][1]['type'] == 'ValidationError'
    assert state_machine.repair_history[0].stage == 'validation'
    assert ExecutionPhase.REPAIR_CODE in [item.phase for item in state_machine.trace]


@pytest.mark.asyncio
async def test_state_machine_repairs_execution_failure(tmp_path, monkeypatch):
    work_dir = tmp_path / 'execution'
    work_dir.mkdir()
    results = [
        SimpleNamespace(
            success=False,
            work_dir=work_dir,
            execution_time_ms=7,
            error_type='RuntimeError',
            error_message='sandbox failed',
            traceback='traceback',
        ),
        SimpleNamespace(success=True, work_dir=work_dir, execution_time_ms=11),
    ]

    orchestrator = FakeOrchestrator(
        executor=FakeExecutor(results),
        code_gen=FakeCodeGen(['print("fixed once")']),
    )

    monkeypatch.setattr('app.agent.state_machine.validate_code', lambda code: (True, None))
    monkeypatch.setattr('app.agent.state_machine.analyze_code', lambda code: [])
    monkeypatch.setattr(
        'app.agent.state_machine.classify',
        lambda error_type, message, traceback, gate=None: SimpleNamespace(key='runtime_error', fix_path=FixPath.CODE),
    )

    state_machine = ExecutionStateMachine(
        orchestrator=orchestrator,
        request_id='req-execution',
        code='print("start")',
        plan=SimpleNamespace(design_brief=SimpleNamespace(), dimensions=None),
        output_formats=['step'],
    )

    response = await state_machine.run()

    assert response.success is True
    assert orchestrator.code_gen.fix_error_calls[0][1]['type'] == 'RuntimeError'
    assert state_machine.repair_history[0].stage == 'execution'
    assert orchestrator.executor.calls[0][1] == '3d'
    assert orchestrator.executor.calls[1][1] == '3d'


@pytest.mark.asyncio
async def test_execute_code_uses_state_machine_without_llm_retry(tmp_path, monkeypatch):
    from app.agent.orchestrator import Orchestrator

    work_dir = tmp_path / 'direct-execute'
    work_dir.mkdir()

    orchestrator = Orchestrator()
    orchestrator.code_gen = FakeCodeGen(['print("should not be used")'])
    orchestrator.executor = FakeExecutor([
        SimpleNamespace(
            success=False,
            work_dir=work_dir,
            execution_time_ms=5,
            error_type='RuntimeError',
            error_message='direct execution failed',
            traceback='traceback',
        )
    ])

    monkeypatch.setattr('app.agent.state_machine.validate_code', lambda code: (True, None))
    monkeypatch.setattr('app.agent.state_machine.analyze_code', lambda code: [])
    monkeypatch.setattr(
        'app.agent.state_machine.classify',
        lambda error_type, message, traceback, gate=None: SimpleNamespace(key='runtime_error', fix_path=FixPath.CODE),
    )

    response = await orchestrator.execute_code('print("direct")')

    assert response.success is False
    assert response.attempts == 1
    assert response.error['message'] == 'direct execution failed'
    assert orchestrator.code_gen.fix_error_calls == []
