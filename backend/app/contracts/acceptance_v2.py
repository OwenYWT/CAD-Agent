"""Versioned predicates; v1 contracts retain their exact serializer and semantics."""
import hashlib
import json
from typing import Annotated, Literal, Union

from pydantic import BeforeValidator, Field, TypeAdapter, model_validator, model_serializer

from app.contracts.acceptance import AcceptanceModel, AcceptanceContract, MeasurementScope
from app.contracts.acceptance_comparison import COMPARISON_POLICY_ID
from app.contracts.hole_populations import population_pools
from app.contracts.assembly_measurements import (
    ASSEMBLY_COUNTS,
    ASSEMBLY_DIMENSIONS,
    ASSEMBLY_PREDICATES,
    AssemblyMeasurement,
    assembly_kind,
)
from app.contracts.machined_feature_values import principal_axis

Quantity = Annotated[float, Field(strict=True, ge=0)]
Unit = Literal['mm', 'mm3', 'count']
_COUNT_KINDS = frozenset({'rectangular_opening_count', 'solid_count', 'hole_count', 'hole_through',
                          'void_connected', 'assembly_count', 'assembly_predicate',
                          'cube_shape', 'sphere_shape', 'cylinder_shape'})


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
    assembly_measurement: AssemblyMeasurement | None = None
    wall_mode: Literal['continuous_normal', 'surface_normal', 'radial', 'local_probe',
                       'side_walls', 'cavity_floor'] = 'continuous_normal'
    frame: Literal['world', 'bounds_center', 'bounds_min', 'selected_object'] = 'world'
    object_ref: SelectedMeasurementObject | None = None
    component_region: ComponentRegion | None = Field(default=None,
        description='Explicit source-authorized world region selecting exactly one whole solid by positive-volume overlap. Never clips geometry or selects by nominal.')

    @model_serializer(mode='wrap')
    def retain_existing_digest(self, handler):
        result = handler(self)
        if self.component_region is None:
            result.pop('component_region', None)
        if self.assembly_measurement is None:
            result.pop('assembly_measurement', None)
        return result

    @model_validator(mode='after')
    def coordinate_authority(self):
        if self.assembly_measurement is not None:
            assembly_kind(self.assembly_measurement)
            if (self.frame != 'world' or self.object_ref or self.component_region or self.axis is not None
                    or self.point_mm is not None or self.centers_mm or self.region_min_mm is not None):
                raise ValueError('assembly roles require the complete independently inventoried assembly')
        if self.frame=='selected_object' and self.object_ref is None:
            raise ValueError('local measurement coordinates require a frozen user-selected object')
        if self.object_ref is not None and self.frame!='selected_object':
            raise ValueError('selected object measurements must retain their authorized local frame')
        if self.component_region is not None and (self.frame != 'world' or self.object_ref is not None):
            raise ValueError('component regions require explicit world coordinates and cannot replace native selection authority')
        return self


class RequirementBindingError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details
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


class VerificationNoticeV1(AcceptanceModel):
    schema_version: Literal['verification-notice.v1'] = 'verification-notice.v1'
    category: Literal['manufacturing_process', 'water_protection', 'physical_performance']
    source_refs: tuple[SourceReference, ...] = Field(min_length=1)
    message: str = Field(min_length=1, max_length=1000)
    status: Literal['unverified'] = 'unverified'


class HoleGroupRef(AcceptanceModel):
    schema_version: Literal['hole-group.v1'] = 'hole-group.v1'
    group_id: str = Field(min_length=1, max_length=512,
        description='Server-owned counted hole phrase identity. Groups jointly partition ALL measured holes in the same scope; this is not a geometry selector.')


class AcceptanceCheckV2(AcceptanceModel):
    check_id: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]*$')
    kind: Literal['solid_count', 'overall_dimension', 'hole_count', 'hole_diameter',
                  'hole_depth', 'hole_through', 'hole_position', 'wall_thickness', 'volume', 'void_connected',
                  'surface_clearance', 'planar_gap', 'bounding_box',
                  'assembly_dimension', 'assembly_count', 'assembly_predicate',
                  'cylinder_diameter', 'cylinder_radius', 'cylinder_height', 'sphere_diameter', 'sphere_radius',
                  'annular_segment_diameter', 'annular_segment_height', 'solid_segment_diameter', 'solid_segment_height',
                  'fillet_radius', 'rectangular_opening_count', 'rectangular_opening_bounds',
                  'semicircular_cut_diameter', 'semicircular_cut_position',
                  'notched_tube_outer_diameter', 'notched_tube_bore_diameter', 'notched_tube_wall_thickness',
                  'cube_edge', 'cube_shape', 'sphere_shape', 'cylinder_shape'] = Field(description=(
        'bounding_box compares zero residual against all six world-coordinate bounds of one whole solid. '
        'For an axis-aligned rectangular prism centered at a specified world position, derive min/max '
        'as center minus/plus half the given dimensions and use scope.component_region. '
        'All cylinder_*, sphere_* and cube_* kinds certify a complete '
        'material primitive, including its entire boundary; they do not apply to a pierced, cut or joined primitive. '
        'overall_dimension measures the complete selected solid(s) along scope.axis; it is not a component '
        'dimension unless scope selects that component. hole_through independently checks the complete shaft passage. '
        'annular_segment_diameter/height measure a complete coaxial annular band selected by a world axis and '
        'an interior point on that axis. solid_segment_diameter/height use the same selector on a solid circular step. '
        'fillet_radius verifies all twelve edges of a world-axis rectangular block, or all four edges parallel '
        'to scope.axis for an extruded rounded rectangle. rectangular_opening_count inventories every straight '
        'rectangular through-opening in a world-axis rectangular plate along scope.axis; rectangular_opening_bounds '
        'compares all six world bounds with region_min_mm/max_mm (eq 0 mm), selecting the opening by its interior midpoint. '
        'semicircular_cut_diameter and semicircular_cut_position certify a single through semicircular edge notch '
        'on a rectangular plate or a transverse half-cylinder cut on a tube. Supply the cutter axis; position compares '
        'zero transverse residual to point_mm on the intended cut axis. notched_tube_outer_diameter, '
        'notched_tube_bore_diameter and notched_tube_wall_thickness measure the tubular stock along scope.axis '
        'with that complete cut accounted for and its bore open end to end. '
        'For segment measurements the axial endpoints come from the actual outer cylindrical face, never '
        'from target dimensions. Use separate checks at each distinct step; bore passage remains a separate check.'))
    description: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)
    source_kind: Literal['user', 'confirmed', 'assumption'] = 'user'
    required: bool = True
    binding_ref: str = Field(min_length=1, description='Supplied quotation reference ID, or saved source_input_id when no quotation reference covers the condition; never invent a source.')
    comparison: Comparison
    scope: MeasurementScopeV2 = Field(default_factory=MeasurementScopeV2)
    hole_group: HoleGroupRef | None = None

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler.resolve_ref_schema(super().__get_pydantic_json_schema__(core_schema, handler))
        def when(kind, **properties):
            return {'if': {'properties': {'kind': {'const': kind}}},
                    'then': {'properties': properties, 'required': list(properties)}}

        schema['allOf'] = [
            when(kind, scope={'required': ['assembly_measurement'], 'properties': {
                'assembly_measurement': {'enum': sorted(names)}, 'frame': {'const': 'world'},
                'axis': {'type': 'null'}, 'point_mm': {'type': 'null'}, 'centers_mm': {'maxItems': 0},
                'region_min_mm': {'type': 'null'}, 'region_max_mm': {'type': 'null'},
                'object_ref': {'type': 'null'}, 'component_region': {'type': 'null'}}},
                comparison={'properties': {'unit': {'const': 'mm' if kind == 'assembly_dimension' else 'count'}}})
            for kind, names in [('assembly_dimension', ASSEMBLY_DIMENSIONS), ('assembly_count', ASSEMBLY_COUNTS),
                                ('assembly_predicate', ASSEMBLY_PREDICATES)]
        ] + [
            {'if': {'required': ['hole_group'], 'properties': {'hole_group': {'type': 'object'}}},
             'then': {'properties': {'kind': {'enum': ['hole_count', 'hole_diameter', 'hole_depth', 'hole_through']}}}},
            when('hole_through', comparison={'properties': {
                'op': {'const': 'eq'}, 'value': {'const': 1}, 'unit': {'const': 'count'}}}),
            # The default empty scope is also valid for planar_gap.
            {'if': {'properties': {'kind': {'const': 'planar_gap'}}}, 'then': {'properties': {
                'scope': {'properties': {'centers_mm': {'maxItems': 0}, 'point_mm': {'type':'null'}}}}}},
            when('void_connected', scope={'required': ['centers_mm', 'region_min_mm', 'region_max_mm'],
                 'description': 'Use at least two distinct void points strictly inside the region on all axes, excluding its boundary.',
                 'properties': {'centers_mm': {'minItems': 2, 'uniqueItems': True},
                                'region_min_mm': {'type':'array'}, 'region_max_mm': {'type':'array'}}}),
        ]
        for kind, fields in (
            ('rectangular_opening_count', ['axis']),
            ('rectangular_opening_bounds', ['axis', 'region_min_mm', 'region_max_mm']),
            ('semicircular_cut_position', ['axis', 'point_mm']),
            ('semicircular_cut_diameter', ['axis']),
            ('notched_tube_outer_diameter', ['axis']), ('notched_tube_bore_diameter', ['axis']),
            ('notched_tube_wall_thickness', ['axis']), ('solid_segment_diameter', ['axis', 'point_mm']),
            ('solid_segment_height', ['axis', 'point_mm']),
        ):
            schema['allOf'].append(when(kind, scope={'required': fields, 'properties': {
                'frame': {'const': 'world'}, **{field: {'type': 'array'} for field in fields}}}))
        # The tool sees the same measurement dimensions and discrete quantities
        # the server enforces when it freezes the returned plan.
        schema['allOf'].append({
            'if': {'properties': {'kind': {'enum': sorted(_COUNT_KINDS)}}},
            'then': {'properties': {'comparison': {'properties': {
                'unit': {'const': 'count'}, **{key: {'type': 'integer'} for key in ('value', 'lower', 'upper')},
                'tolerance_minus': {'const': 0}, 'tolerance_plus': {'const': 0}}}}},
            'else': {'if': {'properties': {'kind': {'const': 'volume'}}},
                     'then': {'properties': {'comparison': {'properties': {'unit': {'const': 'mm3'}}}}},
                     'else': {'properties': {'comparison': {'properties': {'unit': {'const': 'mm'}}}}}},
        })
        return schema

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
        if self.hole_group is not None and kind not in {
            'hole_count', 'hole_diameter', 'hole_depth', 'hole_through'
        }:
            raise ValueError('hole_group applies only to hole population checks')
        if kind.startswith(('annular_segment_', 'solid_segment_')) and (scope.frame != 'world' or scope.axis is None
                or scope.point_mm is None or scope.centers_mm or scope.region_min_mm is not None):
            raise ValueError('annular segment requires a world axis and an interior axis point, without clipping regions')
        count = kind in _COUNT_KINDS
        if kind in {'fillet_radius', 'rectangular_opening_count', 'rectangular_opening_bounds',
                    'semicircular_cut_diameter', 'semicircular_cut_position'} or kind.startswith('notched_tube_'):
            if (scope.frame != 'world' or scope.centers_mm or scope.object_ref
                    or kind != 'fillet_radius' and scope.axis is None
                    or (kind == 'rectangular_opening_bounds') != (scope.region_min_mm is not None)
                    or (kind == 'semicircular_cut_position') != (scope.point_mm is not None)):
                raise ValueError(f'{kind} requires its world feature scope')
            if (kind in {'rectangular_opening_bounds','semicircular_cut_position'} and
                    (not isinstance(comparison, EqualComparison) or comparison.value != 0)):
                raise ValueError(f'{kind} compares zero position residual')
            if kind=='rectangular_opening_bounds' and any(a >= b for a,b in zip(scope.region_min_mm,scope.region_max_mm)):
                raise ValueError('opening bounds must enclose positive volume')
            if scope.axis is not None and (kind == 'fillet_radius' or kind.startswith('rectangular_opening_')):
                principal_axis(scope.axis)
        if kind.startswith('assembly_') or scope.assembly_measurement is not None:
            if assembly_kind(scope.assembly_measurement) != kind:
                raise ValueError('assembly role does not define this measurement kind')
            if kind == 'assembly_predicate' and (not isinstance(comparison, EqualComparison) or comparison.value != 1):
                raise ValueError('assembly relationship requires an exact positive predicate')
        if comparison.unit != ('count' if count else 'mm3' if kind == 'volume' else 'mm'):
            raise ValueError('comparison unit does not match measurement quantity')
        if count:
            if any(v != int(v) for k, v in comparison.model_dump().items() if k in {'value', 'lower', 'upper'}):
                raise ValueError('counts must be integers')
            if isinstance(comparison, EqualComparison) and (comparison.tolerance_minus or comparison.tolerance_plus):
                raise ValueError('counts do not accept engineering tolerance')
        if kind == 'hole_through' and (not isinstance(comparison, EqualComparison) or comparison.value != 1):
            raise ValueError('hole_through requires an exact unobstructed passage predicate (1)')
        if kind in {'cube_shape', 'sphere_shape', 'cylinder_shape'} and (
                not isinstance(comparison, EqualComparison) or comparison.value != 1):
            raise ValueError('material shape requires its complete primitive boundary (1)')
        if kind.startswith('cube_') and scope.axis is not None:
            raise ValueError('cube edges are intrinsic; explicit world-axis extents need separate measurements')
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
        if kind == 'wall_thickness' and scope.wall_mode in {'continuous_normal', 'side_walls', 'cavity_floor'} and (
                scope.point_mm is not None or scope.centers_mm or scope.region_min_mm is not None):
            raise ValueError('whole-part wall coverage cannot use local selectors')
        if kind.startswith(('cylinder_', 'sphere_', 'cube_')) and (
                scope.point_mm is not None or scope.centers_mm or scope.region_min_mm is not None):
            raise ValueError('material primitive dimensions require a complete selected solid, not local probes or clips')
        if kind == 'void_connected' and (not isinstance(comparison, EqualComparison)
                or comparison.value not in {0, 1} or len(scope.centers_mm) < 2 or scope.region_min_mm is None):
            raise ValueError('void connectivity requires exact 0/1, interior probes and region')
        return self


class AcceptanceContractV2(AcceptanceModel):
    schema_version: Literal['engineering-acceptance.v2'] = 'engineering-acceptance.v2'
    objective: str = Field(min_length=1)
    checks: tuple[AcceptanceCheckV2, ...]
    unresolved: tuple[str, ...] = ()
    verification_limits: tuple[str, ...] = Field(default=(), description=(
        'Required conditions that the available geometric measurement tools cannot verify. '
        'Not source-parser diagnostics, export requirements, optional advice or physical-performance disclaimers. '
        'The server separately records unverified manufacturing/material/waterproof performance from saved user intent.'))
    comparison_policy_id: Literal['occt-confusion-v1'] = COMPARISON_POLICY_ID
    verification_scope: Literal['cad_geometry'] = 'cad_geometry'
    source_context: SourceContextV1 | None = None
    verification_notices: tuple[VerificationNoticeV1, ...] = ()

    @model_serializer(mode='wrap')
    def retain_existing_digest(self, handler):
        result = handler(self)
        if self.source_context is None:
            result.pop('source_context', None)
        if not self.verification_notices:
            result.pop('verification_notices', None)
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
        for notice in self.verification_notices:
            if any(ref.source_quote not in self.objective for ref in notice.source_refs):
                raise ValueError('verification notice quote is absent from objective')
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
