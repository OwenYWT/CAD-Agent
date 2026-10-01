"""Versioned measurement requests; independent of planning and execution code."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator, model_serializer


class AcceptanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MeasurementScope(AcceptanceModel):
    """Coordinates in the exported document frame, in millimetres."""
    frame: Literal["world", "bounds_center", "bounds_min"] = "world"
    axis: tuple[float, float, float] | None = Field(default=None,
        description="Numeric direction vector in the document axes; required for overall_dimension and hole_position. Never a text axis name.")
    point_mm: tuple[float, float, float] | None = None
    centers_mm: tuple[tuple[float, float, float], ...] = ()
    region_min_mm: tuple[float, float, float] | None = None
    region_max_mm: tuple[float, float, float] | None = None
    wall_mode: Literal["continuous_normal", "surface_normal", "radial", "local_probe"] = "continuous_normal"
    hole_depth_mode: Literal["total_recess", "shaft_length"] = "total_recess"
    hole_diameter_mode: Literal["shaft", "largest_section"] = "shaft"

    @model_validator(mode="after")
    def valid_geometry(self):
        if self.axis is not None and math.hypot(*self.axis) == 0:
            raise ValueError("measurement axis must be nonzero")
        if (self.region_min_mm is None) != (self.region_max_mm is None):
            raise ValueError("a measurement region requires both corners")
        if self.region_min_mm is not None and any(
            a > b for a, b in zip(self.region_min_mm, self.region_max_mm)
        ):
            raise ValueError("measurement region bounds are inverted")
        return self


class AcceptanceCheck(AcceptanceModel):
    check_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    kind: Literal[
        "solid_count", "overall_dimension", "hole_count", "hole_diameter",
        "hole_depth", "hole_position", "wall_thickness", "volume", "void_connected", "surface_clearance",
    ]
    description: str = Field(min_length=1)
    source_quote: str = Field(min_length=1,
        description="One contiguous verbatim substring of the user's objective. No paraphrase, ellipsis or concatenated fragments. Use the original whole sentence when a check combines clauses.")
    source_kind: Literal["user", "confirmed", "assumption"] = "user"
    required: bool = True
    nominal: float | None = Field(default=None, strict=True,
        description="Omit or null for hole_position (coordinates are scope.centers_mm); required nonnegative scalar for every other kind.")
    tolerance_mm: float | None = Field(default=None, ge=0)
    tolerance_mm3: float | None = Field(default=None, ge=0)
    scope: MeasurementScope = Field(default_factory=MeasurementScope)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """Expose the same variant rules to consumers that runtime validation uses.

        A flat nullable nominal field otherwise advertises values which the
        model validator rejects, especially for coordinate-based measurements.
        """
        # Validators may cause the handler to return a $ref (not an inline
        # object), notably with the deployed Pydantic 2.7 dependency. Resolve it
        # through the public schema handler before adding field constraints.
        schema=handler.resolve_ref_schema(handler(core_schema))
        # Requiredness follows provenance when omitted. Advertising a universal
        # true default would promote an unconfirmed design assumption.
        schema['properties']['required'].pop('default',None)
        schema['allOf']=[
            {'if':{'required':['source_kind'],'properties':{'source_kind':{'const':'assumption'}}},
             'then':{'properties':{'required':{'const':False}}},
             'else':{'properties':{'required':{'const':True}}}},
            {'if':{'properties':{'kind':{'const':'surface_clearance'}}},
             'then':{'required':['scope'],'properties':{'scope':{'required':['centers_mm'],
                 'properties':{'centers_mm':{'minItems':2,'maxItems':2}}}}}},
            {'if':{'properties':{'kind':{'const':'hole_position'}}},
             'then':{'required':['scope'], 'properties':{'nominal':{'type':'null'},
                 'scope':{'required':['axis','centers_mm'], 'properties':{
                     'axis':{'type':'array'},'centers_mm':{'minItems':1}}}}},
             'else':{'required':['nominal'],'properties':{'nominal':{'type':'number','minimum':0}}}},
            {'if':{'properties':{'kind':{'enum':['solid_count','hole_count','void_connected']}}},
             'then':{'properties':{'nominal':{'type':'integer'},'tolerance_mm':{'type':'null'}}}},
            {'if':{'properties':{'kind':{'const':'overall_dimension'}}},
             'then':{'required':['scope'],'properties':{'scope':{'required':['axis'],'properties':{'axis':{'type':'array'}}}}}},
            {'if':{'properties':{'kind':{'const':'volume'}}},
             'then':{'properties':{'tolerance_mm':{'type':'null'}}},
             'else':{'properties':{'tolerance_mm3':{'type':'null'}}}},
            {'if':{'properties':{'kind':{'const':'void_connected'}}},
             'then':{'required':['scope'],'properties':{'nominal':{'enum':[0,1]},'scope':{
                 'required':['centers_mm','region_min_mm','region_max_mm'],'properties':{
                     'centers_mm':{'minItems':2},'region_min_mm':{'type':'array'},'region_max_mm':{'type':'array'}}}}}},
            {'if':{'properties':{'kind':{'const':'wall_thickness'},
                                 'scope':{'properties':{'wall_mode':{'const':'continuous_normal'}}}}},
             'then':{'properties':{'scope':{'properties':{
                 'point_mm':{'type':'null'},'centers_mm':{'maxItems':0},
                 'region_min_mm':{'type':'null'},'region_max_mm':{'type':'null'}}}}}},
        ]
        return schema

    @model_validator(mode='before')
    @classmethod
    def provenance_default(cls, value):
        if isinstance(value,dict) and 'required' not in value:
            return {**value,'required':value.get('source_kind','user')!='assumption'}
        return value

    @model_validator(mode="after")
    def measurement_definition(self):
        if self.source_kind in {'user','confirmed'} and not self.required:
            raise ValueError('an explicit requirement cannot be downgraded to optional')
        if self.source_kind=='assumption' and self.required:
            raise ValueError('an unconfirmed assumption cannot become a required check')
        if self.kind=='wall_thickness' and self.scope.wall_mode=='continuous_normal':
            if self.scope.point_mm is not None or self.scope.centers_mm or self.scope.region_min_mm is not None:
                raise ValueError('continuous_normal covers the whole part; point/region selectors require an explicitly local measurement mode')
        if self.kind == "surface_clearance" and len(self.scope.centers_mm) != 2:
            raise ValueError("surface clearance requires two explicit points identifying distinct faces")
        if self.kind == "volume" and self.tolerance_mm is not None:
            raise ValueError("volume requires cubic millimetre tolerance")
        if self.kind != "volume" and self.tolerance_mm3 is not None:
            raise ValueError("cubic millimetre tolerance is only valid for volume")
        if self.kind == "hole_position":
            if not self.scope.centers_mm or self.scope.axis is None:
                raise ValueError("hole positions require centers and an axis")
            if self.nominal is not None:
                raise ValueError("hole positions use centers, not a scalar nominal")
        elif self.nominal is None or self.nominal < 0:
            raise ValueError("measurement requires a nonnegative nominal")
        if self.kind in {"solid_count", "hole_count", "void_connected"}:
            if self.nominal != int(self.nominal):
                raise ValueError("counts must be integers")
            if self.tolerance_mm is not None:
                raise ValueError("counts are exact and have no millimetre tolerance")
        if self.kind == "void_connected":
            if self.nominal not in {0,1} or len(self.scope.centers_mm)<2 or self.scope.region_min_mm is None:
                raise ValueError("void connectivity requires expected 0/1, interior probes and an explicit region")
        if self.kind == "overall_dimension" and self.scope.axis is None:
            raise ValueError("overall dimension requires an explicit axis")
        return self


class AcceptanceContract(AcceptanceModel):
    schema_version: Literal["engineering-acceptance.v1"] = "engineering-acceptance.v1"
    objective: str = Field(min_length=1)
    checks: tuple[AcceptanceCheck, ...]
    unresolved: tuple[str, ...] = ()
    verification_limits: tuple[str, ...] = Field(default=(),
        description="Required checks the current verifier cannot perform. These are capability gaps, not questions the user can answer. Never replace supported checks with this field.")

    @model_serializer(mode='wrap')
    def preserve_existing_contract(self, handler):
        result = handler(self)
        if not self.verification_limits:
            result.pop('verification_limits', None)
        return result

    @model_validator(mode="after")
    def provenance(self):
        if not any(c.required for c in self.checks) and not self.unresolved and not self.verification_limits:
            raise ValueError("acceptance requires explicit criteria or unresolved requirements")
        ids = [c.check_id for c in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("acceptance check IDs must be unique")
        for check in self.checks:
            if check.source_kind in {"user", "confirmed"} and check.source_quote not in self.objective:
                raise ValueError(f"checks.{check.check_id}.source_quote: user requirement quote is absent from the objective; use a contiguous verbatim source, without ellipsis or joined fragments")
        return self

    def digest(self) -> str:
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


# Concrete input variants make mandatory fields visible directly in each tool
# alternative. A flat nullable nominal/optional scope plus allOf alone suggested
# invalid inputs to real providers. Canonical persisted contracts stay unchanged.
class _AxisScope(MeasurementScope):
    axis: tuple[float, float, float]


class _PositionScope(_AxisScope):
    centers_mm: tuple[tuple[float,float,float], ...] = Field(min_length=1)


class _VoidScope(MeasurementScope):
    centers_mm: tuple[tuple[float,float,float], ...] = Field(min_length=2)
    region_min_mm: tuple[float,float,float]
    region_max_mm: tuple[float,float,float]


class _FacePairScope(MeasurementScope):
    centers_mm: tuple[tuple[float,float,float], ...] = Field(min_length=2,max_length=2)


class _ToolCheck(AcceptanceCheck):
    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema=super().__get_pydantic_json_schema__(core_schema,handler)
        # Each concrete variant below expresses the kind-specific constraints
        # directly. Retain the common provenance condition without repeating
        # every other kind's conditions in every alternative.
        schema['allOf']=[rule for rule in schema['allOf']
                         if 'source_kind' in rule['if'].get('properties',{})]
        return schema


class _ScalarCheck(_ToolCheck):
    kind: Literal['hole_diameter','hole_depth']
    nominal: float = Field(ge=0,strict=True)
    tolerance_mm3: None = None


class _CountCheck(_ToolCheck):
    kind: Literal['solid_count','hole_count']
    nominal: float = Field(ge=0,strict=True,json_schema_extra={'type':'integer'})
    tolerance_mm: None = None
    tolerance_mm3: None = None


class _VolumeCheck(_ToolCheck):
    kind: Literal['volume']
    nominal: float = Field(ge=0,strict=True)
    tolerance_mm: None = None


class _DimensionCheck(_ToolCheck):
    kind: Literal['overall_dimension']
    nominal: float = Field(ge=0,strict=True)
    scope: _AxisScope
    tolerance_mm3: None = None


class _PositionCheck(_ToolCheck):
    kind: Literal['hole_position']
    nominal: None = None
    scope: _PositionScope
    tolerance_mm3: None = None


class _VoidCheck(_ToolCheck):
    kind: Literal['void_connected']
    nominal: Literal[0,1]
    scope: _VoidScope
    tolerance_mm: None = None
    tolerance_mm3: None = None


class _ClearanceCheck(_ToolCheck):
    kind: Literal['surface_clearance']
    nominal: float = Field(ge=0,strict=True)
    scope: _FacePairScope
    tolerance_mm3: None = None


class _WholeWallScope(MeasurementScope):
    wall_mode: Literal['continuous_normal'] = 'continuous_normal'
    point_mm: None = None
    centers_mm: tuple[()] = ()
    region_min_mm: None = None
    region_max_mm: None = None


class _SurfaceWallScope(MeasurementScope):
    wall_mode: Literal['surface_normal']
    point_mm: tuple[float,float,float]


class _LocalWallScope(_AxisScope):
    wall_mode: Literal['local_probe']
    point_mm: tuple[float,float,float]


class _RadialWallScope(MeasurementScope):
    wall_mode: Literal['radial']


class _WallCheck(_ToolCheck):
    kind: Literal['wall_thickness']
    nominal: float = Field(ge=0,strict=True)
    tolerance_mm3: None = None
    scope: Annotated[Union[_WholeWallScope,_SurfaceWallScope,_LocalWallScope,_RadialWallScope],
                     Field(discriminator='wall_mode')] = Field(default_factory=_WholeWallScope)


def acceptance_tool_schema():
    """Discriminated tool view of the same versioned measurement definitions."""
    schema=TypeAdapter(Annotated[Union[_ScalarCheck,_CountCheck,_VolumeCheck,_DimensionCheck,
                                      _PositionCheck,_VoidCheck,_ClearanceCheck,_WallCheck],
        Field(discriminator='kind')]).json_schema()
    definitions=schema.pop('$defs')
    return schema,definitions


class MeasurementEvidence(AcceptanceModel):
    check_id: str
    outcome: Literal["passed", "failed", "indeterminate"]
    method: str = Field(min_length=1)
    measured: tuple[float, ...] = ()
    issues: tuple[str, ...] = ()
    details: dict = Field(default_factory=dict)


class AcceptanceMeasurements(AcceptanceModel):
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: tuple[MeasurementEvidence, ...]


def acceptance_outcome(contract: AcceptanceContract, evidence: tuple[MeasurementEvidence, ...]) -> str:
    """Missing, duplicate or unknown evidence cannot silently authorize a candidate."""
    by_id = {e.check_id: e for e in evidence}
    expected = {c.check_id for c in contract.checks}
    if len(by_id) != len(evidence) or set(by_id) != expected or contract.unresolved or contract.verification_limits:
        return "indeterminate"
    required = [by_id[c.check_id].outcome for c in contract.checks if c.required]
    if "failed" in required:
        return "failed"
    if "indeterminate" in required:
        return "indeterminate"
    return "passed"
