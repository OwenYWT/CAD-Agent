"""Bounded, read-only L2 queries over integrity-verified native kernel state."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.execution.canonical import canonical_sha256
from app.freecad.reference_geometry import normalize_reference_state


class InspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    objects: list[str] = Field(min_length=1, max_length=8)
    fields: list[Literal["properties", "constraints", "geometry", "topology", "dependencies"]] = Field(min_length=1, max_length=5)
    offset: int = Field(default=0, ge=0, le=5000, strict=True)
    limit: int = Field(default=16, ge=1, le=32, strict=True)


def inspect_state(state: dict, request: InspectionRequest) -> dict:
    state = normalize_reference_state(state)
    by_name = {o["name"]: o for o in state.get("objects", [])}
    result = []
    budget = 16000
    for name in dict.fromkeys(request.objects):
        obj = by_name.get(name)
        if obj is None:
            result.append({"name": name, "error": "object_not_found"})
            continue
        detail = {"name": name, "type_id": obj.get("type_id"), "is_valid": obj.get("is_valid")}
        if obj.get("reference_geometry"):
            detail["reference_geometry"] = obj["reference_geometry"]
        for field in dict.fromkeys(request.fields):
            if field == "properties":
                values = [{"name": k, "value": str(v)[:500] if isinstance(v,str) else v}
                          for k,v in obj.get("properties", {}).items()]
                known = len(values)
            elif field == "dependencies":
                values = obj.get("out", [])
                known = len(values)
            else:
                stored = obj.get("inspection", {}).get(field)
                if stored is None:
                    detail[field] = {"status": "unavailable", "reason": "not_recorded_in_kernel_checkpoint"}
                    continue
                if stored.get("status") == "not_applicable":
                    detail[field] = dict(stored)
                    continue
                values = stored["items"]
                known = stored["total"]
            page = []
            for item in values[request.offset:request.offset+request.limit]:
                size = len(json.dumps(item,ensure_ascii=False).encode("utf-8"))
                if size > budget:
                    break
                page.append(item)
                budget -= size
            detail[field] = {"status": "measured", "items": page, "total": known,
                             "offset": request.offset, "returned": len(page),
                             "omitted": max(0,known-len(page)), "recorded": len(values)}
        result.append(detail)
    return {"schema_version": "cad-inspection.v1", "state_hash": canonical_sha256(state),
            "source": "verified_kernel_checkpoint", "objects": result}
