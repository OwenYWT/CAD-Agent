"""Hermetic end-to-end harness for the CAD Agent pipeline.

Runs the FULL real pipeline (Orchestrator.generate/modify/execute_code/handle_message,
the retry loop, _copy_output_files, _extract_params, and the REAL GeometryValidator
printability gate) WITHOUT Docker or a real LLM, by faking ONLY the two external
boundaries:
  - LLM:    FakePlanner / FakeCodeGen / FakeVision  (scriptable per-call queues)
  - Docker: FakeExecutor  (writes a REAL trimesh STL into work_dir/output/ so the
            real geometry validator actually runs)

The fakes match the real interface contracts so the orchestrator runs unchanged.
"""
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import trimesh

from app.agent.orchestrator import Orchestrator
from app.models.schemas import CADPlan, ModificationPlan
from app.sandbox.executor import SandboxResult


# --- real STL builders (so the printability gate is genuinely exercised) -------

def make_stl(kind: str = "printable", out: Path | None = None) -> Path:
    """Write a real STL of a given kind into `out` (or a temp file)."""
    if kind == "printable":           # watertight, fits build volume
        mesh = trimesh.creation.box(extents=(20, 30, 40))
    elif kind == "oversized":         # watertight but exceeds 256mm build volume
        mesh = trimesh.creation.box(extents=(400, 20, 20))
    elif kind == "non_watertight":    # has a hole → not sliceable
        mesh = trimesh.creation.box(extents=(20, 20, 20))
        mesh.update_faces([i for i in range(len(mesh.faces)) if i != 0])
    elif kind == "thin":              # watertight, sub-0.8mm wall (advisory warn)
        mesh = trimesh.creation.box(extents=(40, 40, 0.4))
    elif kind == "tiny":              # near-zero volume → degenerate error
        mesh = trimesh.creation.box(extents=(0.01, 0.01, 0.01))
    else:
        raise ValueError(f"unknown stl kind: {kind}")
    if out is None:
        f = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
        out = Path(f.name)
    mesh.export(out)
    return out


# --- fakes --------------------------------------------------------------------

@dataclass
class FakeExecutor:
    """Stands in for CadQueryExecutor. Each execute() pops the next scripted outcome
    and, on success, writes a REAL STL (+ stub STEP) into work_dir/output/ so that
    _find_stl_in_output / _copy_output_files / GeometryValidator all run for real.

    outcomes: list of dicts, one per expected execute() call:
      {"success": True, "stl": "printable"|"oversized"|"non_watertight"|"thin"|"tiny"}
      {"success": False, "error_type": "...", "error_message": "...", "traceback": "..."}
      {"success": True, "dxf": True}   # 2D path: writes result.dxf
    If the queue empties, the last outcome repeats (so MAX_RETRIES loops terminate).
    """
    outcomes: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    async def execute(self, code: str, mode: str = "3d", extra_files=None) -> SandboxResult:
        self.calls.append({"code": code, "mode": mode})
        outcome = self.outcomes[len(self.calls) - 1] if len(self.calls) <= len(self.outcomes) else self.outcomes[-1]

        work_dir = Path(tempfile.mkdtemp(prefix="cad_e2e_"))
        out = work_dir / "output"
        out.mkdir(parents=True, exist_ok=True)

        if not outcome.get("success"):
            return SandboxResult(
                success=False, files={},
                error_type=outcome.get("error_type", "ExecutionError"),
                error_message=outcome.get("error_message", "boom"),
                traceback=outcome.get("traceback", "Traceback ..."),
                execution_time_ms=5, work_dir=work_dir,
            )

        files = {}
        if outcome.get("dxf"):
            (out / "result.dxf").write_text("0\nSECTION\n")  # minimal stub
            files["dxf"] = "/sandbox/output/result.dxf"
        else:
            stl_path = make_stl(outcome.get("stl", "printable"), out / "result.stl")
            (out / "result.step").write_text("ISO-10303-21;\n")  # stub STEP
            files["stl"] = "/sandbox/output/result.stl"
            files["step"] = "/sandbox/output/result.step"

        return SandboxResult(
            success=True, files=files,
            error_type=None, error_message=None, traceback=None,
            execution_time_ms=5, work_dir=work_dir,
        )


@dataclass
class FakePlanner:
    """Stands in for Planner. Returns scripted CADPlan(s); never touches the LLM."""
    plan: CADPlan | None = None
    mod_plan: ModificationPlan | None = None

    async def plan_new(self, messages):
        if self.plan is not None:
            return self.plan
        return CADPlan(
            description=messages[-1]["content"] if messages else "",
            part_type="custom", dimensions={}, features=[],
            constraints=[], ambiguities=[], modeling_hint="extrude_cut",
        )

    async def plan_modification(self, messages, current_code):
        if self.mod_plan is not None:
            return self.mod_plan
        return ModificationPlan(
            description=messages[-1]["content"] if messages else "",
            modification_type="dimension_change",
        )


@dataclass
class FakeCodeGen:
    """Stands in for CodeGenerator. Returns scripted code strings per method.
    `code` is the default for generate/modify/fix_error; queues let a test assert
    that fix_error was called (and feed a 'fixed' version)."""
    code: str = "w = 20  # 宽\nh = 30  # 高\nresult = box(w, h)\nshow_object(result)"
    code_2d: str = "import ezdxf\ndoc = ezdxf.new()\ndoc.saveas('/sandbox/output/result.dxf')"
    fix_queue: list = field(default_factory=list)
    fix_calls: list = field(default_factory=list)
    generate_calls: int = 0

    async def generate(self, plan, examples, conversation, extra_context=""):
        self.generate_calls += 1
        return self.code

    async def generate_2d(self, plan, examples, conversation):
        return self.code_2d

    async def generate_single_part(self, name, description, dimensions, examples):
        return f"# part {name}\nresult = box(10, 10)\nshow_object(result)"

    async def generate_assembly_combiner(self, parts):
        return "assy = Assembly()\nresult = assy\nshow_object(result)"

    async def modify(self, plan, existing_code, examples, conversation):
        return self.code

    async def fix_error(self, code, error, plan=None):
        self.fix_calls.append(error)
        if self.fix_queue:
            return self.fix_queue.pop(0)
        return self.code  # default: return the same code (still 'fixed')

    async def fix_visual_issues(self, code, issues, suggestions, on_step=None):
        self.fix_calls.append({"type": "visual", "issues": issues})
        if self.fix_queue:
            return self.fix_queue.pop(0)
        return self.code


@dataclass
class FakeVisionResult:
    is_match: bool | None = True  # None = indeterminate (C2)
    confidence: float = 0.9
    issues: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)


@dataclass
class FakeVision:
    """Stands in for VisionValidator. Default: always matches (skip vision retries)."""
    result: FakeVisionResult = field(default_factory=FakeVisionResult)

    async def validate(self, prompt, render_paths, code):
        return self.result


@dataclass
class FakeRenderer:
    """Stands in for CADRenderer. Returns no images → orchestrator skips vision cleanly
    (unless a test wants vision, in which case set FakeVision + return a path)."""
    paths: list = field(default_factory=list)

    def render_stl(self, stl_path, renders_dir):
        return self.paths


@dataclass
class FakeRetriever:
    examples: list = field(default_factory=list)

    async def find_similar(self, query, top_k=3, **criteria):
        return self.examples


def patch_single_step(monkeypatch):
    """Force the simple-part path: make PlanDecomposer.decompose return complexity='simple'
    so generate() takes the single-step branch deterministically (no real LLM, no noise).
    Patches the symbol where it's imported (inside orchestrator.generate)."""
    from app.agent import multi_step

    async def _fake_decompose(self, plan):
        return multi_step.BuildPlan(
            steps=[multi_step.BuildStep(phase=multi_step.BuildPhase.BASE, description=plan.description)],
            complexity="simple",
        )

    monkeypatch.setattr(multi_step.PlanDecomposer, "decompose", _fake_decompose)


def build_orchestrator(
    *, plan=None, code=None, executor_outcomes=None, vision=None,
    fix_queue=None, renderer_paths=None, mod_plan=None,
) -> Orchestrator:
    """Construct a real Orchestrator with all LLM/Docker boundaries faked.
    GeometryValidator stays REAL."""
    orch = Orchestrator.__new__(Orchestrator)  # skip __init__ (which builds real deps)
    orch.MAX_RETRIES = Orchestrator.MAX_RETRIES
    orch.planner = FakePlanner(plan=plan, mod_plan=mod_plan)
    orch.code_gen = FakeCodeGen(
        code=code or FakeCodeGen.code,
        fix_queue=list(fix_queue or []),
    )
    orch.executor = FakeExecutor(outcomes=list(executor_outcomes or [{"success": True, "stl": "printable"}]))
    orch.retriever = FakeRetriever()
    # REAL geometry validator — this is the whole point
    from app.validation.geometry_validator import GeometryValidator
    orch.geometry_validator = GeometryValidator()
    orch.vision_validator = vision or FakeVision()
    orch.renderer = FakeRenderer(paths=list(renderer_paths or []))
    from app.agent.code_cache import CodeCache
    orch.code_cache = CodeCache()
    return orch
