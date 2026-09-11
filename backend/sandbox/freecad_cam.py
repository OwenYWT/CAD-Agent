"""Measured 2.5D external contour machining for a single vertical prism.

The target BRep supplies the silhouette. OCC offsets the cutter centerline;
every emitted cutting segment is checked against the native target outline.
The GRBL program has explicit stock, cutter, feed, spindle and G54 origin data.
"""
import json
import math
from pathlib import Path

import FreeCAD as App
import Part

from freecad_engineering import EngineeringError, _number


def contour_milling(shape, params, directory):
    tool=params['tool'];name=tool.get('name')
    if not isinstance(name,str) or not name.strip() or len(name)>120:
        raise EngineeringError('cam_tool_invalid','请明确指定平底立铣刀')
    diameter=_number(tool['diameter_mm'],'cutter diameter',minimum=0.01,maximum=1000)
    cutting_length=_number(tool['cutting_length_mm'],'cutting length',minimum=0.01,maximum=1000)
    stepdown=_number(params['stepdown_mm'],'axial stepdown',minimum=0.001,maximum=cutting_length)
    feed=_number(params['feed_mm_min'],'cutting feed',minimum=0.01,maximum=100000)
    plunge=_number(params['plunge_mm_min'],'vertical feed',minimum=0.01,maximum=100000)
    spindle=_number(params['spindle_rpm'],'spindle speed',minimum=1,maximum=100000)
    safe_height=_number(params['safe_height_mm'],'safe height',minimum=0.1,maximum=1000)
    stock_margin=_number(params['stock_margin_mm'],'stock margin',minimum=0.1,maximum=1000)
    allowance=_number(params['radial_allowance_mm'],'radial allowance',minimum=0,maximum=100)
    tolerance=_number(params['chord_tolerance_mm'],'chord tolerance',minimum=0.002,maximum=0.25)
    origin=params['work_origin_mm']
    if not isinstance(origin,list) or len(origin)!=3:
        raise EngineeringError('cam_origin_invalid','需要明确的 G54 工件原点')
    origin=[_number(v,'work origin',minimum=-1e6,maximum=1e6) for v in origin]
    if params.get('postprocessor')!='grbl_1_1':
        raise EngineeringError('cam_postprocessor_unsupported','当前只支持 GRBL 1.1 的毫米、绝对坐标直线插补程序')
    bb=shape.BoundBox;height=bb.ZLength
    if height<=1e-6 or height>cutting_length:
        raise EngineeringError('cam_tool_reach','刀具有效刃长必须覆盖所选实体的全高')
    top_faces=[f for f in shape.Faces if isinstance(f.Surface,Part.Plane)
        and abs(f.CenterOfMass.z-bb.ZMax)<1e-7 and abs(f.normalAt(0,0).z)>1-1e-8]
    if len(top_faces)!=1:
        raise EngineeringError('cam_profile_unsupported','外轮廓加工需要单一水平顶面的竖直等截面实体')
    face=top_faces[0];prism=face.extrude(App.Vector(0,0,-height))
    difference=shape.cut(prism).Volume+prism.cut(shape).Volume
    if difference>max(1e-7,shape.Volume*1e-7):
        raise EngineeringError('cam_profile_unsupported','模型不是沿 Z 方向的等截面实体；不能用此路径代替三维曲面加工')
    radius=diameter/2;outline=Part.Face(face.OuterWire)
    # Extra tolerance prevents inward chord sag and six-decimal G-code rounding
    # from cutting into the requested finished silhouette.
    offset=face.OuterWire.makeOffset2D(radius+allowance+2*tolerance,join=0,fill=False)
    if offset.ShapeType!='Wire' or not offset.isClosed() or not offset.isValid():
        raise EngineeringError('cam_offset_invalid','刀具偏置产生多个或无效轮廓，请调整刀径或使用其他加工策略')
    points=offset.discretize(Deflection=tolerance/2)
    if not 4<=len(points)<=5000 or (points[0]-points[-1]).Length>1e-6:
        raise EngineeringError('cam_path_budget','轮廓无法闭合或离散点超过预算')
    points=points[:-1]
    # Start at the globally rightmost cutter position. This makes the lead-in
    # from outside the declared stock independently verifiable against BRep.
    start=max(range(len(points)),key=lambda i:points[i].x)
    points=points[start:]+points[:start];points.append(points[0])
    lead=App.Vector(bb.XMax+stock_margin+radius+safe_height,points[0].y,bb.ZMax)
    path_xy=[lead,*points,lead]
    min_clearance=float('inf')
    for a,b in zip(path_xy,path_xy[1:]):
        # Verify the rounded coordinates actually emitted in the NC file.
        a=App.Vector(round(a.x-origin[0],6)+origin[0],round(a.y-origin[1],6)+origin[1],bb.ZMax)
        b=App.Vector(round(b.x-origin[0],6)+origin[0],round(b.y-origin[1],6)+origin[1],bb.ZMax)
        if (a-b).Length<=1e-9:continue
        distance=Part.makeLine(a,b).distToShape(outline)[0]
        min_clearance=min(min_clearance,distance-radius)
        if distance+1e-6<radius+allowance:
            raise EngineeringError('cam_target_gouge','实际输出的刀具轨迹侵入原生目标轮廓，拒绝发布程序')
    passes=math.ceil(height/stepdown)
    if passes>256 or passes*(len(path_xy)+4)>200000:
        raise EngineeringError('cam_path_budget','轴向层数或路径段数超过预算')
    depths=[max(bb.ZMin,bb.ZMax-(i+1)*stepdown) for i in range(passes)]
    safe_z=bb.ZMax+safe_height
    lines=['(CAD Agent external contour - GRBL 1.1)',
        '(Set G54 to the declared work origin; install the specified flat-end cutter.)',
        '(Target clearance checked; fixtures, machine limits and physical setup are not verified.)',
        'G21 G90 G17 G94 G40 G49 G80','G54',f'S{spindle:.0f} M3']
    trajectory=[];distance_by_motion={'rapid':0.0,'feed':0.0};feed_seconds=0.0
    def move(mode,x=None,y=None,z=None,rate=None):
        nonlocal feed_seconds
        coordinates=[x,y,z]
        previous=trajectory[-1]['position_mm'] if trajectory else None
        current=[round(v-origin[i],6) if v is not None else (previous[i] if previous else None) for i,v in enumerate(coordinates)]
        line=mode+''.join(f' {axis}{current[i]:.6f}' for i,axis in enumerate('XYZ') if coordinates[i] is not None)
        if rate is not None:line+=f' F{rate:.6f}'
        lines.append(line)
        if all(v is not None for v in current):
            motion='rapid' if mode=='G0' else 'feed'
            if previous is not None:
                length=math.sqrt(sum((a-b)**2 for a,b in zip(current,previous)))
                distance_by_motion[motion]+=length
                if motion=='feed':feed_seconds+=length/rate*60
            trajectory.append({'motion':motion,'position_mm':current,'feed_mm_min':rate})
    # The first move is only Z; no horizontal motion occurs before safe height.
    move('G0',z=safe_z)
    move('G0',x=lead.x,y=lead.y,z=safe_z)
    for depth in depths:
        move('G1',z=depth,rate=plunge)
        for point in points:move('G1',x=point.x,y=point.y,rate=feed)
        move('G1',x=lead.x,y=lead.y,rate=feed)
        move('G0',z=safe_z)
    lines+=['M5','M2']
    program=directory/'external-contour.nc';program.write_text('\n'.join(lines)+'\n')
    shape.exportStep(str(directory/'component.step'))
    vertices,triangles=shape.tessellate(max(tolerance,0.02))
    if len(vertices)>100000 or len(triangles)>200000:
        raise EngineeringError('cam_preview_budget','目标几何超过加工预览预算')
    field={'schema_version':'cad-cam-toolpath.v1','units':'mm','postprocessor':'grbl_1_1','work_origin_mm':origin,
        'positions_mm':[[v[i]-origin[i] for i in range(3)] for v in vertices], 'triangles':[list(t) for t in triangles],
        'trajectory':trajectory,'safe_z_mm':safe_z-origin[2],'cutter_diameter_mm':diameter}
    report={'schema_version':'cad-engineering-report.v1','kind':'contour_milling','units':{'length':'mm','feed':'mm/min','spindle':'rpm'},
        'tool':tool,'postprocessor':'grbl_1_1','work_origin_mm':origin,'stepdown_mm':stepdown,'feed_mm_min':feed,
        'plunge_mm_min':plunge,'spindle_rpm':spindle,'safe_height_mm':safe_height,'radial_allowance_mm':allowance,
        'chord_tolerance_mm':tolerance,'stock_margin_mm':stock_margin,
        'declared_stock_bounds_mm':[bb.XMin-stock_margin,bb.YMin-stock_margin,bb.ZMin,bb.XMax+stock_margin,bb.YMax+stock_margin,bb.ZMax],
        'passes':passes,'depths_mm':[v-origin[2] for v in depths],'segments':len(trajectory)-1,
        'minimum_target_clearance_mm':min_clearance,'feed_path_length_mm':distance_by_motion['feed'],
        'rapid_path_length_mm':distance_by_motion['rapid'],'feed_time_seconds':feed_seconds,
        'internal_loops_not_machined':len(face.Wires)-1,'verified_target_prism_volume_difference_mm3':difference,
        'scope':'单实体 Z 向等截面外轮廓、分层铣削；内部孔和口袋不在此工序中加工。已验证刀具对目标轮廓的间隙，未验证夹具、机床行程或实际装夹。',
        'toolpath_engine':'FreeCAD OpenCascade native offsets and BRep clearance'}
    (directory/'toolpath.json').write_text(json.dumps(field,allow_nan=False))
    return report,field,program
