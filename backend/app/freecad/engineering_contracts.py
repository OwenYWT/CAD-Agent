"""Explicit units, material and boundary conditions for native engineering jobs."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel,ConfigDict,Field,TypeAdapter,model_validator

from app.freecad.contracts import ObjectName


class EngineeringContract(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True,allow_inf_nan=False)


class ElasticMaterial(EngineeringContract):
    name: str=Field(min_length=1,max_length=120)
    young_modulus_mpa: float=Field(ge=1e-6,le=1e9)
    poisson_ratio: float=Field(ge=-0.99,le=0.499)

    @model_validator(mode='after')
    def material_name(self):
        if not self.name.strip(): raise ValueError('请填写材料名称')
        return self


class BoundaryPlane(EngineeringContract):
    axis: Literal['x','y','z']
    side: Literal['min','max']


class LinearStaticTask(EngineeringContract):
    kind: Literal['linear_static']='linear_static'
    component_name: ObjectName
    material: ElasticMaterial
    mesh_size_mm: float=Field(ge=0.001,le=1e6)
    fixed_face: BoundaryPlane
    loaded_face: BoundaryPlane
    force_n: tuple[float,float,float]

    @model_validator(mode='after')
    def load_and_support(self):
        if not any(self.force_n) or any(abs(v)>1e12 for v in self.force_n):
            raise ValueError('载荷必须非零，且每个分量不超过 1e12 N')
        if self.fixed_face==self.loaded_face:
            raise ValueError('加载面与固定面必须不同')
        return self


class FlatEndCutter(EngineeringContract):
    name: str=Field(min_length=1,max_length=120)
    diameter_mm: float=Field(ge=0.01,le=1000)
    cutting_length_mm: float=Field(ge=0.01,le=1000)


class ContourMillingTask(EngineeringContract):
    kind: Literal['contour_milling']='contour_milling'
    component_name: ObjectName
    tool: FlatEndCutter
    postprocessor: Literal['grbl_1_1']
    work_origin_mm: tuple[float,float,float]
    stepdown_mm: float=Field(ge=0.001,le=1000)
    feed_mm_min: float=Field(ge=0.01,le=100000)
    plunge_mm_min: float=Field(ge=0.01,le=100000)
    spindle_rpm: int=Field(ge=1,le=100000,strict=True)
    safe_height_mm: float=Field(ge=0.1,le=1000)
    stock_margin_mm: float=Field(ge=0.1,le=1000)
    radial_allowance_mm: float=Field(ge=0,le=100)
    chord_tolerance_mm: float=Field(ge=0.002,le=0.25)

    @model_validator(mode='after')
    def valid_setup(self):
        if not self.tool.name.strip() or self.stepdown_mm>self.tool.cutting_length_mm or any(abs(v)>1e6 for v in self.work_origin_mm):
            raise ValueError('刀具名称、轴向切深或工件原点无效')
        return self


EngineeringTask=Annotated[LinearStaticTask | ContourMillingTask,Field(discriminator='kind')]
ENGINEERING_TASK=TypeAdapter(EngineeringTask)


class EngineeringSubmission(EngineeringContract):
    expected_revision_id: UUID
    expected_state_version: int=Field(ge=0,strict=True)
    idempotency_key: str=Field(min_length=1,max_length=120)
    task: EngineeringTask


ENGINEERING_OUTPUTS = {'engineering_report':'application/json','engineering_field':'application/json',
    'engineering_bundle':'application/zip'}
ENGINEERING_ARTIFACTS={**ENGINEERING_OUTPUTS,'cam_program':'text/plain'}


def engineering_outputs(kind):
    if kind=='release_package':
        from app.freecad.release_contracts import RELEASE_OUTPUTS
        return RELEASE_OUTPUTS
    return ENGINEERING_ARTIFACTS if kind=='contour_milling' else ENGINEERING_OUTPUTS


def engineering_permissions(kind):
    from app.domain.projects import Permission
    return (Permission.COMMIT_VERSION,Permission.EXPORT_ARTIFACT) if kind=='release_package' else (Permission.RUN_VALIDATION,)
