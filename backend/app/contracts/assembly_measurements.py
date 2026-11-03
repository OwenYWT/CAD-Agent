"""Bounded assembly quantities shared with the isolated measurement worker.

Names define quantities, never geometry selected by nominal dimensions or CAD
labels. The independent BRep inventory supplies all values and relationships.
"""
import math

ASSEMBLY_DIMENSIONS = frozenset({
    'envelope_length','envelope_width','envelope_height','side_wall_thickness',
    'floor_thickness','lid_thickness','tab_length','tab_width','tab_thickness',
    'mounting_hole_diameter','connector_diameter','groove_width','groove_depth',
})
ASSEMBLY_COUNTS = frozenset({'tab_count','mounting_hole_count'})
ASSEMBLY_PREDICATES = frozenset({
    'body_lid_fit','tabs_at_corners','mounting_holes_on_tabs','mounting_holes_centered',
    'mounting_holes_isolated','groove_at_interface','connector_through',
    'connector_short_side_centered','envelope_excludes_tabs',
})


def assembly_kind(measurement):
    if measurement in ASSEMBLY_DIMENSIONS:
        return 'assembly_dimension'
    if measurement in ASSEMBLY_COUNTS:
        return 'assembly_count'
    if measurement in ASSEMBLY_PREDICATES:
        return 'assembly_predicate'
    raise ValueError('unknown assembly measurement')


def assembly_values(cert,tolerance):
    """Recompute displayed values from the complete measured feature inventory."""
    b,c,l=cert['body_bounds_mm'],cert['cavity_bounds_mm'],cert['lid_bounds_mm']
    tabs,holes,connector,groove=cert['tabs'],cert['mounting_holes'],cert['connector'],cert['groove']
    close=lambda x,y:abs(x-y)<=tolerance
    fit=all(close(b[i],l[i]) for i in [0,1,3,4]) and close(b[5],l[2]) and cert['coverage']['component_overlap_mm3']==0
    envelope=[min(b[a],l[a]) for a in range(3)]+[max(b[a],l[a]) for a in range(3,6)]
    lengths=[envelope[i+3]-envelope[i] for i in range(3)]
    corners=[]
    for tab in tabs:
        bounds=tab['bounds_mm'];axis=tab['attachment_axis'];other=1-axis;side=tab['attachment_side']
        corner=-1 if close(bounds[other],b[other]) else 1 if close(bounds[other+3],b[other+3]) else 0
        corners.append((side,corner) if axis==0 else (corner,side))
    on_tabs=(bool(tabs) and len(holes)==len(tabs) and sorted(h['tab_index'] for h in holes)==list(range(len(tabs)))
        and all(h['through']==1 and close(h['interval_mm'][0],tabs[h['tab_index']]['bounds_mm'][2])
                and close(h['interval_mm'][1],tabs[h['tab_index']]['bounds_mm'][5]) for h in holes))
    centered=on_tabs and all(all(close(h['origin_mm'][a],
        (tabs[h['tab_index']]['bounds_mm'][a]+tabs[h['tab_index']]['bounds_mm'][a+3])/2) for a in [0,1]) for h in holes)
    short_axis=0 if b[3]-b[0]>b[4]-b[1] else 1
    side_centered=(not close(b[3]-b[0],b[4]-b[1]) and close(connector['axis'][short_axis],1.)
        and all(close(connector['origin_mm'][a],(envelope[a]+envelope[a+3])/2) for a in [1-short_axis,2]))
    go,gi=groove['outer_bounds_mm'],groove['inner_bounds_mm']
    ring_inside=all(b[a]<go[a]<gi[a]<c[a] and c[a+3]<gi[a+3]<go[a+3]<b[a+3] for a in [0,1])
    values={
        'envelope_length':[max(lengths[:2])],'envelope_width':[min(lengths[:2])],'envelope_height':[lengths[2]],
        'side_wall_thickness':[c[0]-b[0],c[1]-b[1],b[3]-c[3],b[4]-c[4]],
        'floor_thickness':[c[2]-b[2]],'lid_thickness':[l[5]-l[2]],
        'tab_length':[t['bounds_mm'][t['attachment_axis']+3]-t['bounds_mm'][t['attachment_axis']] for t in tabs],
        'tab_width':[t['bounds_mm'][4-t['attachment_axis']]-t['bounds_mm'][1-t['attachment_axis']] for t in tabs],
        'tab_thickness':[t['bounds_mm'][5]-t['bounds_mm'][2] for t in tabs],
        'tab_count':[len(tabs)],'mounting_hole_count':[len(holes)],
        'mounting_hole_diameter':[h['diameter_mm'] for h in holes],'connector_diameter':[connector['diameter_mm']],
        'groove_width':[gi[0]-go[0],gi[1]-go[1],go[3]-gi[3],go[4]-gi[4]],
        'groove_depth':[b[5]-groove['floor_z_mm']],
        'body_lid_fit':[int(fit)],
        'tabs_at_corners':[int(len(tabs)==4 and set(corners)=={(-1,-1),(-1,1),(1,-1),(1,1)})],
        'mounting_holes_on_tabs':[int(on_tabs)],'mounting_holes_centered':[int(centered)],
        'mounting_holes_isolated':[int(on_tabs and all(h['cavity_clearance_mm']>tolerance for h in holes))],
        'groove_at_interface':[int(fit and ring_inside and c[2]<groove['floor_z_mm']<b[5])],
        'connector_through':[connector['through']],'connector_short_side_centered':[int(side_centered)],
        'envelope_excludes_tabs':[int(fit and bool(tabs) and all(
            (t['bounds_mm'][t['attachment_axis']+3]<=b[t['attachment_axis']]+tolerance if t['attachment_side']==-1
             else t['bounds_mm'][t['attachment_axis']]>=b[t['attachment_axis']+3]-tolerance) for t in tabs))],
    }
    return values


def verify_assembly_certificate(check,evidence):
    if not check['kind'].startswith('assembly_') or evidence['outcome']=='indeterminate':return
    details=evidence['details'];cert=details.get('rectangular_assembly') or {}
    tolerance=details.get('numerical_tolerance_mm')
    def numbers(values,length=None):
        return (isinstance(values,list) and (length is None or len(values)==length)
            and all(type(v) in (int,float) and math.isfinite(v) for v in values))
    if (cert.get('schema_version')!='rectangular-assembly.v1' or cert.get('complete') is not True
            or cert.get('selection_uses_nominal') is not False or not numbers([tolerance]) or not 0<tolerance<=1e-7):
        raise ValueError('assembly certificate is incomplete or uses unsupported selection/precision')
    coverage=cert.get('coverage') or {};count=coverage.get('face_count')
    if (coverage.get('solid_count')!=2 or type(count) is not int or count<1
            or coverage.get('covered_face_indices')!=list(range(count))
            or len(coverage.get('face_types',[]))!=count or not set(coverage['face_types'])<={'PLANE','CYLINDER'}
            or any(type(coverage.get(k)) not in (int,float) or coverage[k]!=0 for k in
                   ['body_missing_mm3','body_extra_mm3','lid_missing_mm3','lid_extra_mm3'])
            or coverage.get('method')!='complete_observed_material_reconstruction_with_enumerated_voids'):
        raise ValueError('assembly material boundary coverage is incomplete')
    for key in ['body_bounds_mm','cavity_bounds_mm','lid_bounds_mm']:
        bounds=cert.get(key)
        if not numbers(bounds,6) or any(b<=a for a,b in zip(bounds[:3],bounds[3:])):
            raise ValueError('assembly bounds are invalid')
    frame=cert.get('frame',{})
    axes=[frame.get(key) for key in ['x_axis','y_axis','z_axis']]
    if (not numbers(frame.get('origin_mm'),3) or any(not numbers(a,3) for a in axes)
            or any(abs(sum(a*b for a,b in zip(axes[i],axes[j]))-int(i==j))>1e-9 for i in range(3) for j in range(3))):
        raise ValueError('assembly local frame is not orthonormal')
    tabs=cert.get('tabs');holes=cert.get('mounting_holes');groove=cert.get('groove',{})
    if not isinstance(tabs,list) or not isinstance(holes,list):raise ValueError('assembly feature inventory is missing')
    for tab in tabs:
        bounds=tab.get('bounds_mm')
        if (not numbers(bounds,6) or any(b<=a for a,b in zip(bounds[:3],bounds[3:]))
                or tab.get('attachment_axis') not in [0,1] or tab.get('attachment_side') not in [-1,1]):
            raise ValueError('invalid tab inventory')
    for h in [*holes,cert.get('connector',{})]:
        if (not numbers([h.get('diameter_mm')]) or h['diameter_mm']<=0 or not numbers(h.get('axis'),3)
                or abs(math.hypot(*h['axis'])-1)>1e-9 or not numbers(h.get('origin_mm'),3)
                or not numbers(h.get('interval_mm'),2) or h['interval_mm'][0]>=h['interval_mm'][1]
                or type(h.get('through')) is not int or h['through'] not in [0,1]
                or not h.get('face_indices') or any(type(i) is not int or not 0<=i<count for i in h['face_indices'])):
            raise ValueError('invalid passage inventory')
        proof=h.get('through_proof') or {}
        if (proof.get('axis')!=h['axis'] or proof.get('origin_mm')!=h['origin_mm']
                or proof.get('shaft_diameter_mm')!=h['diameter_mm'] or proof.get('passage_interval_mm')!=h['interval_mm']):
            raise ValueError('passage inventory differs from its section evidence')
        through=int(proof.get('blocking_solids')==0 and proof.get('blocking_material_mm3')==0
            and proof.get('blocked_end_area_mm2')==[0,0] and proof.get('end_sections_connected_to_exterior')==[True,True])
        if through!=h['through']:raise ValueError('passage result differs from complete section coverage')
    if any(type(h.get('tab_index')) is not int or not 0<=h['tab_index']<len(tabs)
           or not numbers([h.get('cavity_clearance_mm')]) or h['cavity_clearance_mm']<0 for h in holes):
        raise ValueError('mounting hole ownership or cavity separation is absent')
    if (not numbers(groove.get('outer_bounds_mm'),6) or not numbers(groove.get('inner_bounds_mm'),6)
            or not numbers([groove.get('floor_z_mm')])):
        raise ValueError('complete sealing ring bounds are absent')
    values=assembly_values(cert,tolerance)
    if cert.get('values')!=values or evidence['measured']!=values[check['scope']['assembly_measurement']]:
        raise ValueError('assembly measurement disagrees with complete independent inventory')
    binding=details.get('measurement_binding')
    if binding is not None and binding.get('proof',{}).get('selection',{}).get('rectangular_assembly')!=cert:
        raise ValueError('assembly inventory differs from artifact-bound evidence')
