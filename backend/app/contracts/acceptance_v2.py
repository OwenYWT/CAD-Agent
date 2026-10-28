"""Versioned predicates; v1 contracts retain their exact serializer and semantics."""
import hashlib
import json
from typing import Annotated, Literal, Union

from pydantic import BeforeValidator, Field, TypeAdapter, model_validator, model_serializer

from app.contracts.acceptance import AcceptanceModel, AcceptanceContract, MeasurementScope
from app.contracts.acceptance_comparison import COMPARISON_POLICY_ID
from app.contracts.hole_populations import population_pools

Quantity = Annotated[float, Field(strict=True, ge=0)]
Unit = Literal['mm', 'mm3', 'count']


class SelectedMeasurementObject(AcceptanceModel):
    revision_id: str
    feature_id: str
    kernel_name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,79}$')
    type_id: Literal['PartDesign::Body', 'App::Link']
    parameter_state_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class ComponentRegion(AcceptanceModel):
    min_mm: tuple[float, float, float]
    max_mm: tuple[float, float, float]

    @model_validator(mode='after')
    def positive_volume(self):
        if any(a >= b for a, b in zip(self.min_mm, self.max_mm)):
            raise ValueError('component region must have positive volume')
        return self


class MeasurementScopeV2(MeasurementScope):
    frame: Literal['world', 'bounds_center', 'bounds_min', 'selected_object'] = 'world'
    object_ref: SelectedMeasurementObject | None = None
    component_region: ComponentRegion | None = Field(default=None,
        description='Explicit source-authorized world region selecting exactly one whole solid by positive-volume overlap. Never clips geometry or selects by nominal.')

    @model_serializer(mode='wrap')
    def retain_existing_digest(self, handler):
        result = handler(self)
        if self.component_region is None:
            result.pop('component_region', None)
        return result

    @model_validator(mode='after')
    def coordinate_authority(self):
        if self.frame=='selected_object' and self.object_ref is None:
            raise ValueError('local measurement coordinates require a frozen user-selected object')
        if self.object_ref is not None and self.frame!='selected_object':
            raise ValueError('selected object measurements must retain their authorized local frame')
        if self.component_region is not None and (self.frame != 'world' or self.object_ref is not None):
            raise ValueError('component regions require explicit world coordinates and cannot replace native selection authority')
        return self


class RequirementBindingError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(code + ': ' + message)


class SourceRequirementUnavailable(RequirementBindingError):
    """A source capability/authority failure cannot be repaired by another LLM call."""


class EqualComparison(AcceptanceModel):
    op: Literal['eq']
    value: Quantity
    unit: Unit
    tolerance_minus: Quantity = 0.
    tolerance_plus: Quantity = 0.
    tolerance_source_refs: tuple[str, ...] = ()

    @model_validator(mode='after')
    def tolerance_authority(self):
        if (self.tolerance_minus or self.tolerance_plus) and not self.tolerance_source_refs:
            raise ValueError('requirement_tolerance_unbound: positive engineering tolerance requires a verified source')
        return self


class LowerComparison(AcceptanceModel):
    op: Literal['ge']
    lower: Quantity
    unit: Unit


class UpperComparison(AcceptanceModel):
    op: Literal['le']
    upper: Quantity
    unit: Unit


class RangeComparison(AcceptanceModel):
    op: Literal['range']
    lower: Quantity
    upper: Quantity
    unit: Unit

    @model_validator(mode='after')
    def ordered(self):
        if self.lower > self.upper:
            raise ValueError('comparison lower bound exceeds upper bound')
        return self


Comparison = Annotated[Union[EqualComparison, LowerComparison, UpperComparison, RangeComparison],
                       Field(discriminator='op')]


class SourceReference(AcceptanceModel):
    reference_id: str = Field(min_length=1)
    source_input_id: str = Field(min_length=1)
    source_kind: Literal['user', 'confirmed']
    source_span: tuple[int, int]
    source_quote: str = Field(min_length=1)


class DesignRequestReference(SourceReference):
    # A request for one CAD design can contain an assembly of multiple solids.
    # Delivery is the existing single branch/revision/document transaction;
    # this is deliberately not a geometric solid-count assertion.
    count: Literal[1]
    delivery_unit: Literal['cad_document'] = 'cad_document'


class ExternalDimensionReference(SourceReference):
    property: Literal['plate_thickness']
    comparison: Comparison

    @model_validator(mode='after')
    def length_input(self):
        if self.comparison.unit != 'mm':
            raise ValueError('external plate thickness requires a length')
        return self


class SourceContextV1(AcceptanceModel):
    schema_version: Literal['source-context.v1'] = 'source-context.v1'
    execution_scope: Literal['cad_geometry', 'concept_geometry'] = 'cad_geometry'
    design_requests: tuple[DesignRequestReference, ...] = ()
    external_references: tuple[ExternalDimensionReference, ...] = ()
    unverified_aspects: tuple[Literal['physical_fit', 'clamping_force'], ...] = ()

    @model_validator(mode='after')
    def bounded_context(self):
        refs = [r.reference_id for r in (*self.design_requests, *self.external_references)]
        if len(refs) != len(set(refs)):
            raise ValueError('source context references must be unique')
        if self.external_references and (self.execution_scope != 'concept_geometry'
                or self.unverified_aspects != ('physical_fit', 'clamping_force')):
            raise ValueError('external clearance does not certify physical fit or clamping force')
        return self


class HoleGroupRef(AcceptanceModel):
    schema_version: Literal['hole-group.v1'] = 'hole-group.v1'
    group_id: str = Field(min_length=1, max_length=512,
        description='Server-owned counted hole phrase identity. Groups jointly partition ALL measured holes in the same scope; this is not a geometry selector.')


class AcceptanceCheckV2(AcceptanceModel):
    check_id: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]*$')
    kind: Literal['solid_count', 'overall_dimension', 'hole_count', 'hole_diameter',
                  'hole_depth', 'hole_through', 'hole_position', 'wall_thickness', 'volume', 'void_connected',
                  'surface_clearance', 'planar_gap', 'bounding_box']
    description: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)
    source_kind: Literal['user', 'confirmed', 'assumption'] = 'user'
    required: bool = True
    binding_ref: str = Field(min_length=1, description='ID from the server extracted requirements; never invent an ID.')
    comparison: Comparison
    scope: MeasurementScopeV2 = Field(default_factory=MeasurementScopeV2)
    hole_group: HoleGroupRef | None = None

    @model_serializer(mode='wrap')
    def retain_existing_digest(self, handler):
        result = handler(self)
        if self.hole_group is None:
            result.pop('hole_group', None)
        return result

    @model_validator(mode='after')
    def measurement_definition(self):
        if self.required != (self.source_kind != 'assumption'):
            raise ValueError('requiredness must match verified provenance')
        kind, scope, comparison = self.kind, self.scope, self.comparison
        count = kind in {'solid_count', 'hole_count', 'hole_through', 'void_connected'}
        if comparison.unit != ('count' if count else 'mm3' if kind == 'volume' else 'mm'):
            raise ValueError('comparison unit does not match measurement quantity')
        if count:
            if any(v != int(v) for k, v in comparison.model_dump().items() if k in {'value', 'lower', 'upper'}):
                raise ValueError('counts must be integers')
            if isinstance(comparison, EqualComparison) and (comparison.tolerance_minus or comparison.tolerance_plus):
                raise ValueError('counts do not accept engineering tolerance')
        if kind == 'hole_through' and (not isinstance(comparison, EqualComparison) or comparison.value != 1):
            raise ValueError('hole_through requires an exact unobstructed passage predicate (1)')
        if kind == 'hole_position':
            if not scope.centers_mm or scope.axis is None:
                raise ValueError('hole positions require centers and an axis')
            if not isinstance(comparison, EqualComparison) or comparison.value != 0:
                raise ValueError('hole_position compares zero transverse position residual')
        if kind == 'overall_dimension' and scope.axis is None:
            raise ValueError('overall dimension requires an explicit axis')
        if kind == 'bounding_box' and (scope.component_region is None
                or not isinstance(comparison, EqualComparison) or comparison.value != 0):
            raise ValueError('bounding_box compares zero residual against source-authorized component bounds')
        if kind == 'surface_clearance' and len(scope.centers_mm) != 2:
            raise ValueError('surface clearance requires two explicit face points')
        if kind == 'planar_gap' and (scope.centers_mm or scope.point_mm is not None):
            raise ValueError('planar_gap requires a unique face pair, not guessed surface points')
        if kind == 'wall_thickness' and scope.wall_mode == 'continuous_normal' and (
                scope.point_mm is not None or scope.centers_mm or scope.region_min_mm is not None):
            raise ValueError('whole-part wall coverage cannot use local selectors')
        if kind == 'void_connected' and (not isinstance(comparison, EqualComparison)
                or comparison.value not in {0, 1} or len(scope.centers_mm) < 2 or scope.region_min_mm is None):
            raise ValueError('void connectivity requires exact 0/1, interior probes and region')
        return self


class AcceptanceContractV2(AcceptanceModel):
    schema_version: Literal['engineering-acceptance.v2'] = 'engineering-acceptance.v2'
    objective: str = Field(min_length=1)
    checks: tuple[AcceptanceCheckV2, ...]
    unresolved: tuple[str, ...] = ()
    verification_limits: tuple[str, ...] = ()
    comparison_policy_id: Literal['occt-confusion-v1'] = COMPARISON_POLICY_ID
    verification_scope: Literal['cad_geometry'] = 'cad_geometry'
    source_context: SourceContextV1 | None = None

    @model_serializer(mode='wrap')
    def retain_existing_digest(self, handler):
        result = handler(self)
        if self.source_context is None:
            result.pop('source_context', None)
        return result

    @model_validator(mode='after')
    def provenance(self):
        if not any(c.required for c in self.checks) and not self.unresolved and not self.verification_limits:
            raise ValueError('acceptance requires explicit criteria or unresolved requirements')
        if len({c.check_id for c in self.checks}) != len(self.checks):
            raise ValueError('acceptance check IDs must be unique')
        for check in self.checks:
            if check.source_kind != 'assumption' and check.source_quote not in self.objective:
                raise ValueError(f'checks.{check.check_id}.source_quote is absent from objective')
        if self.source_context:
            for ref in (*self.source_context.design_requests, *self.source_context.external_references):
                if ref.source_quote not in self.objective:
                    raise ValueError('source context quote is absent from objective')
        grouped = [c.model_dump(mode='json') for c in self.checks if c.hole_group is not None]
        if grouped:
            population_pools(grouped)
        return self

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode='json'), ensure_ascii=False,
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _default_legacy_version(value):
    if isinstance(value, dict) and 'schema_version' not in value:
        return {'schema_version': 'engineering-acceptance.v1', **value}
    return value


VersionedAcceptance = Annotated[Union[AcceptanceContract, AcceptanceContractV2],
    Field(discriminator='schema_version'), BeforeValidator(_default_legacy_version)]


def decode_acceptance(value):
    if isinstance(value, (AcceptanceContract, AcceptanceContractV2)):
        return value
    # Old fixtures and stored v1 objects may omit the version's default.
    if isinstance(value, dict) and 'schema_version' not in value:
        return AcceptanceContract.model_validate(value)
    return TypeAdapter(VersionedAcceptance).validate_python(value)
