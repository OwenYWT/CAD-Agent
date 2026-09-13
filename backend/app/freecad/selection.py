"""Versioned user selection and validation against measured native state."""
from __future__ import annotations

import re
from uuid import UUID

from app.domain.selection import SelectionContextV1
from app.topology.contracts import FreeCADTopologySelector


class SelectionError(ValueError):
    code = "selection_invalid"


_DEICTIC = re.compile(r"(?:这|那|此|该)(?:一)?(?:个|条|处)?(?:孔|边|面|特征|草图)|\b(?:this|that|selected)\s+(?:hole|edge|face|feature|sketch)\b", re.I)
_HOLE = re.compile(r"(?:这|那|此|该)(?:一)?个?孔|\b(?:this|that|one|selected)\s+hole\b", re.I)
_EDGE_FACE = re.compile(r"(?:这|那|此|该)(?:一)?(?:条|个)?(?:边|面)|\b(?:this|that|selected)\s+(?:edge|face)\b", re.I)


def needs_selection(objective: str) -> bool:
    return bool(_DEICTIC.search(objective))


def freeze_selection(projection: dict, selection: SelectionContextV1, *,
                     revision_id: UUID, state_version: int, objective: str) -> dict:
    if selection.revision_id != revision_id or selection.state_version != state_version:
        raise SelectionError("选择来自旧版本，请在当前已提交版本中重新选择目标")
    if str(projection.get("revision_id")) != str(revision_id) or not projection.get("fcstd"):
        raise SelectionError("选择目标需要可核验的原生文档检查点")
    by_id = {feature["id"]: feature for feature in projection.get("features", [])}
    if any(str(key) not in by_id for key in selection.feature_ids):
        raise SelectionError("选择的特征不存在于此文档版本，请重新选择")
    features = [by_id[str(key)] for key in selection.feature_ids]
    if any(f.get("type") in {"App::Plane", "App::Line", "App::Point", "App::Origin"} for f in features):
        raise SelectionError("基准显示对象不支持此原生修改，请选择实际建模特征")
    selector = selection.topology_selector
    if selector:
        feature = features[0]
        bindings = [FreeCADTopologySelector.model_validate(b) for b in feature.get("topology_bindings", [])]
        if selector.object_name != feature["kernel_name"] or selector not in bindings:
            raise SelectionError("子元素没有完整且唯一的测量依据，不能猜测 Face/Edge 或邻近位置")
        if not re.search(r"圆角|倒角|\bfillet\b|\bchamfer\b", objective, re.I):
            raise SelectionError("当前子元素选择只支持已测量的圆角或倒角；修改孔径等整特征参数时，请清除子元素选择并明确修改整个特征")
    elif _EDGE_FACE.search(objective):
        raise SelectionError("特征选择不能代替边或面的选择，请选择已测量且唯一的子元素")
    if needs_selection(objective) and len(features) != 1:
        raise SelectionError("指代目标不唯一，请只选择一个明确目标或改写指令")
    if _HOLE.search(objective) and not selector and not re.search(r"所有|全部|整个.*特征|all\s+holes", objective, re.I):
        # A Hole/Pocket may consume a sketch with many profiles. Do not equate
        # a single feature ID with one physical hole, even when its label says so.
        feature = features[0]
        sketches = [f for f in [feature, *(by_id[d] for d in feature.get("dependencies", []) if d in by_id)]
                    if f.get("sketch") is not None]
        if not sketches or any(f["sketch"].get("geometry_count") != 1 for f in sketches):
            raise SelectionError("该特征的单孔身份无法确认；请明确修改整个孔特征，或选择可唯一定位的子元素")
    return {**selection.model_dump(mode="json"),
            "parameter_state_sha256": projection.get("parameter_state_sha256"),
            "features": [{"feature_id": f["id"], "kernel_name": f["kernel_name"],
                          "label": f["label"], "type": f.get("type")} for f in features]}


def validate_selected_operations(plan, base_state: dict) -> None:
    """A provider may inspect the document but cannot silently edit another target."""
    selection = base_state.get("selection_context")
    if not selection:
        return
    objects = {obj["name"]: obj for obj in base_state.get("objects", [])}
    allowed = {f["kernel_name"] for f in selection["features"]}
    if not allowed or not allowed <= objects.keys():
        raise SelectionError("冻结的选择对象已缺失，不能推测替代目标")
    # Containment grants scope only when the user selected that real container.
    # OutList also includes attachment/support geometry: reading it must never
    # grant permission to change the support Pad of a selected Hole.
    pending = list(allowed)
    while pending:
        structure = objects[pending.pop()].get("structure") or {}
        if structure.get("status") != "measured":
            continue
        for member in structure.get("members", []):
            if member in objects and member not in allowed:
                allowed.add(member); pending.append(member)
    bodies = {name for name in allowed if objects[name].get("type_id") == "PartDesign::Body"}
    for name in tuple(allowed):
        if objects[name].get("type_id", "").startswith("PartDesign::"):
            allowed.update(dependency for dependency in objects[name].get("out", [])
                if objects.get(dependency, {}).get("type_id") == "Sketcher::SketchObject")
    new_sketches = set()

    def require_target(name):
        if name not in allowed:
            raise SelectionError(f"操作试图修改选择范围之外的特征 {name}，请重新确认目标")

    def allow_created(name):
        if not name or name in objects or name in allowed:
            raise SelectionError("新增特征名称与现有范围冲突，不能覆盖已有对象")
        allowed.add(name)

    for operation in plan.operations:
        action, args = operation.action, operation.args
        if action in {"document.inspect", "document.export"}:
            continue
        if selection.get("topology_selector") and action not in {"feature.fillet", "feature.chamfer"}:
            raise SelectionError("此操作不能限定到冻结的子元素；不得改写整个特征或其他孔")
        if action == "sketch.create":
            if args.get("body", "Body") not in bodies:
                raise SelectionError("新增草图需要明确选择所属 Body，不能在其他容器中创建几何")
            allow_created(args["name"]); new_sketches.add(args["name"])
        elif action in {"feature.pad", "feature.pocket", "feature.hole"}:
            require_target(args["profile"])
            if args["profile"] not in new_sketches and not bodies:
                raise SelectionError("新增实体特征需要明确选择所属 Body，不能借助依赖改变未选择的实体")
            allow_created(args["name"])
        elif action in {"feature.fillet", "feature.chamfer"}:
            require_target(args["target"]); allow_created(args["name"])
        elif action == "assembly.instance":
            require_target(args["source"]); allow_created(args["object"])
        elif action in {"property.set", "assembly.place"}:
            require_target(args["object"])
        elif action.startswith("sketch."):
            require_target(args["sketch"])
        else:
            raise SelectionError("此操作尚未定义选择范围，不能推测写入目标")
        if selection.get("topology_selector") and action in {"feature.fillet", "feature.chamfer"}:
            if FreeCADTopologySelector.model_validate(args.get("selector") or {}) != FreeCADTopologySelector.model_validate(selection["topology_selector"]):
                raise SelectionError("圆角或倒角必须使用本次冻结的子元素选择，不能替换为全部边")
