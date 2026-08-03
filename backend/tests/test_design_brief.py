from app.agent.design_brief import ensure_design_brief
from app.models.schemas import CADPlan, CriticalDimension, DesignBrief, GenerateResponse, GenerationResult


def test_design_brief_schema_serializes_engineering_fields():
    brief = DesignBrief(
        intent_summary="A printable desk cable clip.",
        artifact_type="clip",
        manufacturing_posture="printable",
        assumptions=["Cable diameter is approximately 6 mm"],
        critical_dimensions=[
            CriticalDimension(
                name="wall_thickness",
                value=2.4,
                unit="mm",
                reason="Six 0.4 mm nozzle lines make a sturdy printable wall",
            )
        ],
        functional_requirements=["Hold cable without pinching"],
        printability_targets=["Minimum wall thickness at least 2.0 mm"],
        acceptance_criteria=["Geometry is watertight"],
        open_questions=["Confirm exact cable diameter if fit is critical"],
    )

    payload = brief.model_dump()

    assert payload["intent_summary"] == "A printable desk cable clip."
    assert payload["artifact_type"] == "clip"
    assert payload["manufacturing_posture"] == "printable"
    assert payload["critical_dimensions"][0]["name"] == "wall_thickness"
    assert payload["critical_dimensions"][0]["value"] == 2.4


def test_cad_plan_accepts_optional_design_brief():
    brief = DesignBrief(intent_summary="A simple bracket", artifact_type="bracket")

    plan = CADPlan(
        description="L bracket",
        part_type="bracket",
        dimensions={"length": 40},
        features=["two mounting holes"],
        design_brief=brief,
    )

    assert plan.design_brief == brief
    assert plan.model_dump()["design_brief"]["intent_summary"] == "A simple bracket"


def test_generation_responses_carry_design_brief():
    brief = DesignBrief(intent_summary="A printable enclosure", artifact_type="enclosure")

    generate_response = GenerateResponse(request_id="req-brief", success=True, design_brief=brief)
    generation_result = GenerationResult(success=True, request_id="req-brief", design_brief=brief)

    assert generate_response.model_dump()["design_brief"]["artifact_type"] == "enclosure"
    assert generation_result.model_dump()["design_brief"]["artifact_type"] == "enclosure"


def test_ensure_design_brief_builds_fallback_from_cad_plan():
    plan = CADPlan(
        description="Adjustable phone stand",
        part_type="stand",
        dimensions={"width": 70, "height": 120, "wall_thickness": 3},
        features=["tilted back support", "rounded base"],
        constraints=["print without support"],
        ambiguities=["phone thickness not specified"],
    )

    brief = ensure_design_brief(plan)

    assert brief.intent_summary == "Adjustable phone stand"
    assert brief.artifact_type == "stand"
    assert brief.manufacturing_posture == "printable"
    assert "按原型件用途和公制尺寸进行设计" in brief.assumptions
    assert [item.name for item in brief.critical_dimensions] == ["width", "height", "wall_thickness"]
    assert brief.critical_dimensions[0].reason == "用户提供或需求分析推断的关键尺寸"
    assert "tilted back support" in brief.functional_requirements
    assert "print without support" in brief.functional_requirements
    assert "几何体封闭且适合继续做打印检查" in brief.acceptance_criteria
    assert brief.open_questions == ["需要适配的手机厚度是多少？"]
    assert plan.design_brief == brief


def test_ensure_design_brief_preserves_existing_brief_and_backfills_empty_lists():
    plan = CADPlan(
        description="Small hinge",
        part_type="hinge",
        dimensions={"pin_diameter": 4},
        features=[],
        design_brief=DesignBrief(intent_summary="A printable hinge", artifact_type="hinge"),
    )

    brief = ensure_design_brief(plan)

    assert brief.intent_summary == "A printable hinge"
    assert brief.artifact_type == "hinge"
    assert brief.printability_targets
    assert brief.acceptance_criteria

from app.agent.planner import Planner


def test_planner_parse_preserves_design_brief_from_json():
    payload = {
        "description": "Desk cable clip",
        "part_type": "clip",
        "dimensions": {"length": 60, "wall_thickness": 2.4},
        "features": ["snap slot"],
        "constraints": ["print without supports where possible"],
        "ambiguities": ["exact cable diameter not specified"],
        "modeling_hint": "extrude_cut",
        "design_brief": {
            "intent_summary": "A small 3D-printable clip that holds one cable against a desk edge.",
            "artifact_type": "clip",
            "manufacturing_posture": "printable",
            "assumptions": ["Cable diameter is approximately 6 mm"],
            "critical_dimensions": [
                {
                    "name": "wall_thickness",
                    "value": 2.4,
                    "unit": "mm",
                    "reason": "Six 0.4 mm nozzle lines give a sturdy printable wall",
                }
            ],
            "functional_requirements": ["Hold cable without pinching"],
            "printability_targets": ["Avoid unsupported overhangs above 45 degrees"],
            "acceptance_criteria": ["Cable channel remains open"],
            "open_questions": ["Confirm exact cable diameter if fit is critical"],
        },
    }

    plan = Planner._parse_plan_payload(payload)
    instance_plan = Planner()._parse_plan_payload(payload)

    assert instance_plan == plan
    assert plan.design_brief is not None
    assert plan.design_brief.intent_summary.startswith("A small 3D-printable clip")
    assert plan.design_brief.critical_dimensions[0].reason.startswith("Six 0.4 mm")


def test_planner_keeps_valid_plan_when_optional_brief_dimension_is_composite():
    payload = {
        "description": "Desktop organizer",
        "part_type": "enclosure",
        "dimensions": {"width": 120, "depth": 90, "height": 60},
        "features": ["three compartments"],
        "constraints": [],
        "ambiguities": [],
        "modeling_hint": "extrude_cut",
        "design_brief": {
            "intent_summary": "A three-compartment organizer",
            "critical_dimensions": [{
                "name": "overall_size",
                "value": "120×90×60",
                "unit": "mm",
                "reason": "Overall envelope",
            }],
        },
    }

    plan = Planner._parse_plan_payload(payload)

    assert plan.dimensions == {"width": 120, "depth": 90, "height": 60}
    assert plan.design_brief is not None
    dimension = plan.design_brief.critical_dimensions[0]
    assert dimension.name == "overall_size"
    assert dimension.value is None
    assert dimension.reason == "Overall envelope"


from app.agent.code_gen import CodeGenerator


def test_code_generator_formats_design_brief_context():
    plan = CADPlan(
        description="Desk cable clip",
        part_type="clip",
        dimensions={"wall_thickness": 2.4},
        features=["snap slot"],
        design_brief=DesignBrief(
            intent_summary="A printable cable clip for a desk edge",
            artifact_type="clip",
            manufacturing_posture="printable",
            assumptions=["Cable diameter is approximately 6 mm"],
            critical_dimensions=[CriticalDimension(name="wall_thickness", value=2.4, unit="mm", reason="Printable sturdy wall")],
            functional_requirements=["Hold cable without pinching"],
            printability_targets=["Avoid unsupported overhangs above 45 degrees"],
            acceptance_criteria=["Cable channel remains open"],
            open_questions=["Confirm cable diameter"],
        ),
    )

    context = CodeGenerator._format_design_brief_context(plan)

    assert "Engineering Brief" in context
    assert "A printable cable clip for a desk edge" in context
    assert "wall_thickness=2.4 mm" in context
    assert "Printable sturdy wall" in context
    assert "Cable channel remains open" in context


def test_generate_response_can_embed_plan_and_brief_together():
    plan = CADPlan(
        description="Printable wall hook",
        part_type="hook",
        dimensions={"width": 30},
        features=["rounded hook"],
    )
    brief = ensure_design_brief(plan)

    response = GenerateResponse(
        request_id="req-flow",
        success=True,
        code="result = cq.Workplane('XY').box(1, 1, 1)",
        plan=plan,
        design_brief=brief,
    )

    payload = response.model_dump()

    assert payload["plan"]["design_brief"]["artifact_type"] == "hook"
    assert payload["design_brief"]["artifact_type"] == "hook"
