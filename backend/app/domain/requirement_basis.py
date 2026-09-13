"""User supplied design basis, never a claim of verified physical fit."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RequirementBasisV1(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    schema_version: Literal['requirement-basis.v1'] = 'requirement-basis.v1'
    target: str = Field(min_length=1, max_length=1000)
    purpose: str = Field(default='', max_length=500)
    source_kind: Literal['none', 'user_measurement', 'reference'] = 'none'
    source_reference: str = Field(default='', max_length=500)
    dimensions: str = Field(default='', max_length=1500)
    fit_notes: str = Field(default='', max_length=1000)
    concept_acknowledged: bool = False

    @model_validator(mode='after')
    def explicit_missing_basis(self):
        if self.source_kind != 'none' and (not self.source_reference or not self.dimensions):
            raise ValueError('请提供关键尺寸及其来源说明，或明确选择概念外形')
        if self.source_kind == 'none' and not self.concept_acknowledged:
            raise ValueError('缺少尺寸依据，请补充来源或明确确认概念外形、适配未验证')
        return self

    def planning_context(self) -> str:
        return '\n'.join([
            '用户确认的工程依据（用户输入，不是独立校验证据）：',
            f'目标：{self.target}', f'用途：{self.purpose or "未确认"}',
            f'尺寸来源类型：{self.source_kind}；来源：{self.source_reference or "未提供"}',
            f'用户明确尺寸：{self.dimensions or "未提供，不得凭机型名称声称尺寸可靠"}',
            f'开孔与装配依据：{self.fit_notes or "未提供"}',
            '不得擅自替换用户明确尺寸；冲突必须请求确认。',
            '适配未验证。缺少依据的几何只能作为概念外形，不得声称适配、配合或制造条件已验证。',
        ])
