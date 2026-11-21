from __future__ import annotations

from app.models.schemas import CADPlan, CriticalDimension, DesignBrief, ManufacturingProfile

_DEFAULT_ACCEPTANCE_CRITERIA = [
    "代码可成功执行",
    "按请求格式导出模型文件",
    "独立检查几何有效性",
]

_ARTIFACT_TYPE_LABELS = {
    "box": "盒体",
    "bracket": "支架",
    "cylinder": "圆柱件",
    "plate": "板件",
    "flange": "法兰",
    "enclosure": "外壳",
    "clip": "夹具",
    "gear": "齿轮",
    "fixture": "工装夹具",
    "stand": "支撑架",
    "assembly": "装配体",
    "profile_2d": "二维轮廓",
    "revolution": "回转体",
    "swept": "扫掠件",
    "organic": "曲面过渡件",
    "custom": "自定义零件",
}

_POSTURE_LABELS = {
    "printable": "面向 3D 打印",
    "cnc": "面向 CNC 加工",
    "sheet metal": "面向钣金加工",
    "injection molding": "面向注塑成型",
}

_GENERIC_QUESTION = "请补充该模型的关键尺寸、用途或安装方式。"


def _has_cjk(value: str) -> bool:
    return any("一" <= char <= "鿿" for char in value)


def _localized_text(value: str, fallback: str) -> str:
    cleaned = value.strip() if value else ""
    if not cleaned:
        return fallback
    return cleaned


def localize_user_question(value: str) -> str:
    cleaned = value.strip() if value else ""
    if not cleaned:
        return _GENERIC_QUESTION
    if _has_cjk(cleaned):
        return cleaned

    lowered = cleaned.lower()
    if "desk" in lowered and "thickness" in lowered:
        return "夹具需要适配的桌面厚度是多少？"
    if "phone" in lowered and "thickness" in lowered:
        return "需要适配的手机厚度是多少？"
    if "cable" in lowered and "diameter" in lowered:
        return "需要适配的线缆直径是多少？"
    if "screw" in lowered and "diameter" in lowered:
        return "螺丝孔直径需要是多少？"
    if "wall" in lowered and "thickness" in lowered:
        return "期望的壁厚是多少？"
    if "dimension" in lowered or "size" in lowered:
        return "请确认模型的关键尺寸范围。"
    if "material" in lowered:
        return "请确认计划使用的材料。"
    if "load" in lowered or "weight" in lowered:
        return "请确认需要承受的载荷或重量。"
    if "fit" in lowered or "mount" in lowered:
        return "请确认需要配合或安装的对象尺寸。"
    return _GENERIC_QUESTION


def _localized_list(values: list[str], fallback: list[str] | None = None) -> list[str]:
    localized: list[str] = []
    for value in values:
        cleaned = value.strip() if value else ""
        if not cleaned:
            continue
        localized.append(cleaned)
    return localized or list(fallback or [])


def _localized_questions(values: list[str]) -> list[str]:
    questions: list[str] = []
    for value in values:
        question = localize_user_question(value)
        if question and question not in questions:
            questions.append(question)
    return questions


def _dimensions_from_plan(plan: CADPlan) -> list[CriticalDimension]:
    return [
        CriticalDimension(
            name=name,
            value=value,
            unit="mm",
            reason="用户提供或需求分析推断的关键尺寸",
        )
        for name, value in plan.dimensions.items()
    ]


def _localized_dimensions(items: list[CriticalDimension]) -> list[CriticalDimension]:
    return [
        CriticalDimension(
            name=item.name,
            value=item.value,
            unit=item.unit or "mm",
            reason=_localized_text(item.reason, "用于约束模型比例和制造可行性"),
        )
        for item in items
    ]


def ensure_design_brief(plan: CADPlan) -> DesignBrief:
    existing = plan.design_brief or DesignBrief()
    assumptions = _localized_list(
        existing.assumptions,
        fallback=["按原型件用途和公制尺寸进行设计"],
    )
    critical_dimensions = _localized_dimensions(existing.critical_dimensions or _dimensions_from_plan(plan))
    functional_requirements = _localized_list(
        existing.functional_requirements or [*plan.features, *plan.constraints],
    )
    printability_targets = _localized_list(existing.printability_targets)
    acceptance_criteria = _localized_list(
        existing.acceptance_criteria,
        fallback=_DEFAULT_ACCEPTANCE_CRITERIA,
    )
    open_questions = _localized_questions(existing.open_questions or plan.ambiguities)
    if existing.acceptance is not None:
        for unresolved in existing.acceptance.unresolved:
            question = "必须确认：" + unresolved
            if question not in open_questions:
                open_questions.append(question)

    raw_artifact_type = plan.part_type if existing.artifact_type == "custom" else existing.artifact_type
    brief = DesignBrief(
        intent_summary=_localized_text(existing.intent_summary, plan.description),
        artifact_type=raw_artifact_type,
        manufacturing_posture=existing.manufacturing_posture or "printable",
        assumptions=assumptions,
        critical_dimensions=critical_dimensions,
        functional_requirements=functional_requirements,
        printability_targets=printability_targets,
        acceptance_criteria=acceptance_criteria,
        open_questions=open_questions,
        acceptance=existing.acceptance,
    )
    plan.design_brief = brief
    return brief


def apply_manufacturing_profile_to_brief(
    brief: DesignBrief, profile: ManufacturingProfile | None
) -> None:
    if not profile:
        return
    process_labels = {
        "fdm": "FDM",
        "sla": "SLA",
        "cnc": "CNC",
        "laser_cut": "激光切割",
        "generic": "通用工艺",
    }
    process_label = process_labels.get(profile.process, profile.process)
    brief.manufacturing_posture = f"{process_label} {profile.material}".strip()
    profile_targets = [
        f"制造工艺：{process_label}",
        f"材料：{profile.material}",
    ]
    if profile.nozzle_diameter_mm is not None:
        profile_targets.append(f"喷嘴直径：{profile.nozzle_diameter_mm:g} mm")
    if profile.layer_height_mm is not None:
        profile_targets.append(f"层高：{profile.layer_height_mm:g} mm")
    if profile.build_volume_mm:
        volume = " x ".join(f"{value:g}" for value in profile.build_volume_mm)
        profile_targets.append(f"成型空间：{volume} mm")
    for target in profile_targets:
        if target not in brief.printability_targets:
            brief.printability_targets.append(target)
