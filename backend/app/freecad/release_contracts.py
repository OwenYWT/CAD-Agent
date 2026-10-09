"""Public release selection and the server-frozen native packaging request."""
from typing import Literal
from uuid import UUID
import json
from pydantic import Field,model_validator,model_serializer
from app.freecad.engineering_contracts import EngineeringContract
from app.freecad.contracts import ObjectName


class ReleaseOptions(EngineeringContract):
    component_names: tuple[ObjectName, ...] = Field(default=(), max_length=1000)
    mesh_precision: Literal['coarse','medium','fine'] = 'medium'
    units: Literal['mm'] = 'mm'

    @model_validator(mode='after')
    def unique_scope(self):
        if len(self.component_names) != len(set(self.component_names)):
            raise ValueError('发布范围不能包含重复部件')
        return self


class ReleaseSubmission(EngineeringContract):
    release_name:str=Field(min_length=1,max_length=120)
    expected_revision_id:UUID
    expected_state_version:int=Field(ge=0,strict=True)
    engineering_workflow_ids:tuple[UUID,...]=Field(default=(),max_length=8)
    idempotency_key:str=Field(min_length=1,max_length=120)
    options: ReleaseOptions = Field(default_factory=ReleaseOptions)

    @model_validator(mode='after')
    def valid_release(self):
        if not self.release_name.strip() or len(set(self.engineering_workflow_ids))!=len(self.engineering_workflow_ids):
            raise ValueError('发布名称不能为空，工程任务不能重复')
        return self


class ReleaseSource(EngineeringContract):
    document_id:UUID
    project_id:UUID
    revision_id:UUID
    state_version:int=Field(ge=0,strict=True)
    fcstd_artifact_id:UUID
    fcstd_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')


class ReleaseEngineeringArtifact(EngineeringContract):
    artifact_id:UUID
    workflow_run_id:UUID
    source_revision_id:UUID
    artifact_kind:Literal['engineering_report','engineering_field','engineering_bundle','cam_program']
    package_filename:str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,239}$')
    size_bytes:int=Field(ge=1,le=128*1024*1024,strict=True)
    sha256:str=Field(pattern=r'^[0-9a-f]{64}$')


class ReleaseTask(EngineeringContract):
    kind:Literal['release_package']='release_package'
    release_name:str=Field(min_length=1,max_length=120)
    source:ReleaseSource
    engineering_artifacts:tuple[ReleaseEngineeringArtifact,...]=Field(default=(),max_length=32)
    annotations:tuple[dict,...]=Field(default=(),max_length=1000)
    options: ReleaseOptions = Field(default_factory=ReleaseOptions)
    manufacturing_profile: dict | None = None
    requirements: dict | None = None

    @model_serializer(mode='wrap')
    def historical_identity(self, handler):
        payload = handler(self)
        if self.options == ReleaseOptions():
            payload.pop('options', None)
        if self.manufacturing_profile is None:
            payload.pop('manufacturing_profile', None)
        if self.requirements is None:
            payload.pop('requirements', None)
        return payload

    @model_validator(mode='after')
    def bounded_source_evidence(self):
        if len(json.dumps(self.annotations,ensure_ascii=False))>256*1024:
            raise ValueError('发布标注超过单次上下文预算')
        if len({a.artifact_id for a in self.engineering_artifacts})!=len(self.engineering_artifacts) or len({a.package_filename for a in self.engineering_artifacts})!=len(self.engineering_artifacts):
            raise ValueError('发布工件重复')
        if any(a.source_revision_id!=self.source.revision_id for a in self.engineering_artifacts):
            raise ValueError('发布只能包含同一修订的工程证据')
        return self


RELEASE_OUTPUTS={'engineering_report':'application/json','engineering_bundle':'application/zip',
    'release_manifest':'application/json','release_bom_json':'application/json','release_bom_csv':'text/csv',
    'release_step':'model/step','release_stl':'model/stl'}
