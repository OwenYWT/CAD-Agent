"""Read-only engineering compute against a selected native solid.

FreeCAD owns geometry; the bundled Gmsh and CalculiX binaries perform real
quadratic tetrahedral meshing and small-displacement linear elasticity.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

import FreeCAD as App

from freecad_scene import component_shapes

GMSH = '/opt/freecad/usr/bin/gmsh'
CCX = '/opt/freecad/usr/bin/ccx'
MAX_NODES = 20000
MAX_ELEMENTS = 50000


class EngineeringError(ValueError):
    def __init__(self, code, message):
        super().__init__(message); self.code = code


def _number(value, name, *, minimum=None, maximum=None):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise EngineeringError('engineering_input_invalid',f'{name} must be finite')
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise EngineeringError('engineering_input_invalid',f'{name} is outside its supported range')
    return float(value)


def _run(command, directory, log_name, timeout=120):
    with (directory/log_name).open('w') as log:
        try:
            completed = subprocess.run(command,cwd=directory,stdout=log,stderr=subprocess.STDOUT,timeout=timeout,check=False)
        except subprocess.TimeoutExpired as exc:
            raise EngineeringError('engineering_solver_timeout','工程计算超过时间预算') from exc
    log = (directory/log_name).read_text(errors='replace')
    if completed.returncode or '*ERROR' in log.upper():
        raise EngineeringError('engineering_solver_failed',f'{Path(command[0]).name} failed: {log[-2200:]}')
    return log


def _mesh_input(path):
    nodes, elements = {}, {}; section = None; pending = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('**'):
            continue
        if line.startswith('*'):
            if pending:
                raise EngineeringError('engineering_mesh_invalid','Incomplete tetrahedral element')
            upper = line.upper()
            section = 'nodes' if upper=='*NODE' else 'elements' if upper.startswith('*ELEMENT,') and 'TYPE=C3D10' in upper.replace(' ','') else None
            continue
        values = [item.strip() for item in line.split(',') if item.strip()]
        if section=='nodes':
            if len(values)!=4:
                raise EngineeringError('engineering_mesh_invalid','Unexpected mesh node record')
            nodes[int(values[0])] = tuple(float(v) for v in values[1:])
        elif section=='elements':
            pending.extend(map(int,values))
            if len(pending)==11:
                elements[pending[0]]=tuple(pending[1:]);pending=[]
            elif len(pending)>11:
                raise EngineeringError('engineering_mesh_invalid','Unexpected tetrahedral connectivity')
    if pending or not nodes or not elements or len(nodes)>MAX_NODES or len(elements)>MAX_ELEMENTS:
        raise EngineeringError('engineering_mesh_budget','网格为空、不完整或超过节点/单元预算，请调整网格尺寸')
    if not all(all(math.isfinite(x) for x in p) for p in nodes.values()) or any(n not in nodes for e in elements.values() for n in e):
        raise EngineeringError('engineering_mesh_invalid','Mesh has invalid coordinates or connectivity')
    return nodes,elements


def _boundary_faces(elements):
    # Abaqus/CalculiX C3D10 node order: four corners, then mids of edges
    # 1-2, 2-3, 3-1, 1-4, 2-4, 3-4. Retain mids for consistent face loads.
    local = [(0,1,2,4,5,6),(0,3,1,7,8,4),(1,3,2,8,9,5),(0,2,3,6,9,7)]
    faces = {}; counts = defaultdict(int)
    for element in elements.values():
        for indices in local:
            face = tuple(element[i] for i in indices);key = tuple(sorted(face[:3]))
            counts[key]+=1;faces[key]=face
    if any(n>2 for n in counts.values()):
        raise EngineeringError('engineering_mesh_invalid','Mesh boundary is non-manifold')
    return [face for key,face in faces.items() if counts[key]==1]


def _face_selection(nodes, faces, selector, bounds):
    if set(selector)!={'axis','side'} or selector['axis'] not in {'x','y','z'} or selector['side'] not in {'min','max'}:
        raise EngineeringError('engineering_boundary_invalid','边界必须指定 X/Y/Z 轴及最小/最大平面')
    axis = 'xyz'.index(selector['axis']);coordinate = bounds[axis+(3 if selector['side']=='max' else 0)]
    tolerance = max(1e-7,max(bounds[i+3]-bounds[i] for i in range(3))*1e-7)
    selected = [f for f in faces if all(abs(nodes[n][axis]-coordinate)<=tolerance for n in f)]
    if not selected:
        raise EngineeringError('engineering_boundary_missing','所选包围盒极值处没有可用的平面边界')
    return selected


def _area(points):
    a,b,c=(App.Vector(*p) for p in points)
    return (b-a).cross(c-a).Length/2


def _reactions(path):
    collecting = False;values = {};blocks = []
    for line in path.read_text(errors='replace').splitlines():
        if 'forces (fx,fy,fz)' in line.lower() and 'support' in line.lower():
            if values: blocks.append(values)
            collecting=True;values={};continue
        if collecting:
            parts=line.split()
            if len(parts)==4 and parts[0].isdigit():
                values[int(parts[0])]=tuple(float(x.replace('D','E')) for x in parts[1:])
            elif values:
                blocks.append(values);values={};collecting=False
    if values: blocks.append(values)
    if not blocks:
        raise EngineeringError('engineering_reactions_missing','求解器未输出支撑反力，无法验证载荷平衡')
    return [sum(v[i] for v in blocks[-1].values()) for i in range(3)]


def _von_mises(stress):
    x,y,z,xy,xz,yz=stress
    return math.sqrt(max(0,((x-y)**2+(y-z)**2+(z-x)**2)/2+3*(xy*xy+xz*xz+yz*yz)))


def linear_static(shape, params, directory):
    material=params['material'];name=material.get('name')
    if not isinstance(name,str) or not name.strip() or len(name)>120:
        raise EngineeringError('engineering_input_invalid','请提供明确的材料名称及弹性参数')
    elastic=_number(material['young_modulus_mpa'],'Young modulus',minimum=1e-6,maximum=1e9)
    poisson=_number(material['poisson_ratio'],'Poisson ratio',minimum=-0.99,maximum=0.499)
    mesh_size=_number(params['mesh_size_mm'],'mesh size',minimum=0.001,maximum=1e6)
    force=params['force_n']
    if not isinstance(force,list) or len(force)!=3:
        raise EngineeringError('engineering_input_invalid','载荷必须是三个以 N 为单位的分量')
    force=[_number(v,'force',minimum=-1e12,maximum=1e12) for v in force]
    force_norm=math.sqrt(sum(v*v for v in force))
    if force_norm==0 or params['fixed_face']==params['loaded_face']:
        raise EngineeringError('engineering_boundary_invalid','载荷不能为零，加载面与固定面必须不同')
    if shape.Volume/mesh_size**3>15000:
        raise EngineeringError('engineering_mesh_budget','请求网格过密，请增大网格尺寸')
    bb=shape.BoundBox;bounds=[bb.XMin,bb.YMin,bb.ZMin,bb.XMax,bb.YMax,bb.ZMax]
    shape.exportStep(str(directory/'component.step'))
    (directory/'mesh.geo').write_text('SetFactory("OpenCASCADE");\nGeometry.OCCTargetUnit="MM";\nMerge "component.step";\n'
        'Physical Volume("Solid") = Volume{:};\n'
        f'Mesh.MeshSizeMin={mesh_size:.12g};\nMesh.MeshSizeMax={mesh_size:.12g};\n'
        'Mesh.ElementOrder=2;\nMesh.SecondOrderLinear=0;\nMesh.SaveGroupsOfNodes=1;\n')
    _run([GMSH,'mesh.geo','-3','-format','inp','-o','mesh.inp','-nt','1','-v','2'],directory,'gmsh.log')
    nodes,elements=_mesh_input(directory/'mesh.inp')
    mesh_bounds=[fn(p[i] for p in nodes.values()) for fn in (min,max) for i in range(3)]
    if max(abs(a-b) for a,b in zip(bounds,mesh_bounds))>max(1e-5,mesh_size*0.2):
        raise EngineeringError('engineering_mesh_units','网格与原生模型的毫米尺寸不一致')
    faces=_boundary_faces(elements)
    fixed=_face_selection(nodes,faces,params['fixed_face'],bounds)
    loaded=_face_selection(nodes,faces,params['loaded_face'],bounds)
    supports=sorted({n for f in fixed for n in f});weights=defaultdict(float)
    for face in loaded:
        area=_area([nodes[n] for n in face[:3]])
        for node in face[3:]: weights[node]+=area/3
    area=sum(weights.values())
    if area<=1e-12:
        raise EngineeringError('engineering_boundary_invalid','加载面没有可测量面积')
    lines=['*HEADING','CAD Agent native linear elasticity, mm N MPa','*NODE']
    lines.extend(f'{n},'+','.join(f'{v:.12g}' for v in p) for n,p in nodes.items())
    lines.append('*ELEMENT, TYPE=C3D10, ELSET=SOLID')
    lines.extend(f'{n},'+','.join(map(str,e)) for n,e in elements.items())
    lines.append('*NSET, NSET=SUPPORT')
    lines.extend(','.join(map(str,supports[i:i+12])) for i in range(0,len(supports),12))
    lines.extend(['*MATERIAL, NAME=MATERIAL','*ELASTIC',f'{elastic:.12g}, {poisson:.12g}',
        '*SOLID SECTION, ELSET=SOLID, MATERIAL=MATERIAL','*BOUNDARY','SUPPORT,1,3,0',
        '*STEP','*STATIC','*CLOAD'])
    lines.extend(f'{node},{axis+1},{value*weight/area:.12g}' for node,weight in weights.items() for axis,value in enumerate(force) if value)
    lines.extend(['*NODE FILE','U','*EL FILE','S','*NODE PRINT, NSET=SUPPORT','RF','*END STEP'])
    (directory/'analysis.inp').write_text('\n'.join(lines)+'\n')
    solver_log=_run([CCX,'-i','analysis'],directory,'calculix.log')
    version=re.search(r'Version\s+([0-9.]+)',solver_log,re.IGNORECASE)
    if version is None:
        raise EngineeringError('engineering_runtime_unknown','成功求解记录中没有 CalculiX 版本信息')
    from feminout.importCcxFrdResults import read_frd_result
    result=read_frd_result(str(directory/'analysis.frd'))
    frames=result['Results']
    if len(frames)!=1 or not frames[0].get('disp') or not frames[0].get('stress'):
        raise EngineeringError('engineering_results_missing','求解器未返回完整的单步位移与应力结果')
    frame=frames[0];displacements=frame['disp'];stresses=frame['stress']
    if set(displacements)!=set(nodes) or set(stresses)!=set(nodes):
        raise EngineeringError('engineering_results_missing','求解结果节点集合与输入网格不一致')
    reactions=_reactions(directory/'analysis.dat')
    balance=math.sqrt(sum((reactions[i]+force[i])**2 for i in range(3)))/force_norm
    if not math.isfinite(balance) or balance>0.001:
        raise EngineeringError('engineering_force_imbalance','支撑反力与施加载荷不平衡，拒绝发布计算结果')
    maxima={'displacement_mm':max(v.Length for v in displacements.values()),'von_mises_mpa':max(_von_mises(s) for s in stresses.values())}
    ids=sorted(nodes);indices={n:i for i,n in enumerate(ids)}
    triangles=[]
    for a,b,c,ab,bc,ca in faces:
        triangles.extend([[indices[n] for n in t] for t in ((a,ab,ca),(ab,b,bc),(ca,bc,c),(ab,bc,ca))])
    data={'schema_version':'cad-fea-field.v1','positions_mm':[list(nodes[n]) for n in ids],
        'displacements_mm':[list(displacements[n]) for n in ids],'von_mises_mpa':[_von_mises(stresses[n]) for n in ids],
        'triangles':triangles}
    report={'schema_version':'cad-engineering-report.v1','kind':'linear_static','material':material,
        'units':{'length':'mm','force':'N','stress':'MPa'},'element_type':'C3D10','nodes':len(nodes),'elements':len(elements),
        'mesh_size_mm':mesh_size,'volume_mm3':shape.Volume,'bounds_mm':bounds,'force_n':force,'reaction_n':reactions,
        'force_balance_relative_error':balance,'fixed_face':params['fixed_face'],'loaded_face':params['loaded_face'],
        'support_node_count':len(supports),'loaded_surface_mesh_area_mm2':area,'maximum':maxima,
        'mean_loaded_displacement_mm':[sum(displacements[n][i]*w for n,w in weights.items())/area for i in range(3)],
        'scope':'单实体、均匀各向同性材料、小变形线弹性；应力为当前二次四面体网格的节点外推值。',
        'solver':{'name':'CalculiX','version':version.group(1)},
        'mesher':{'name':'Gmsh','version':subprocess.check_output([GMSH,'-version'],text=True,stderr=subprocess.STDOUT).strip()}}
    # JSON rejects non-finite solver outputs rather than making a false report.
    json.dumps([report,data],allow_nan=False)
    return report,data


def run_engineering(task, input_root, output_root):
    if task['params'].get('kind')=='release_package':
        from freecad_release import run_release
        return run_release(task,input_root,output_root)
    params=task['params'];name=params.get('component_name')
    if not isinstance(name,str) or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',name) is None:
        raise EngineeringError('engineering_component_invalid','请选择当前版本中的实体部件')
    base_name=task['inputs'].get('base')
    if not isinstance(base_name,str) or Path(base_name).name!=base_name:
        raise EngineeringError('engineering_source_invalid','需要有效的原生检查点输入')
    source=input_root/base_name
    if not source.is_file() or source.is_symlink() or source.suffix.lower()!='.fcstd':
        raise EngineeringError('engineering_source_invalid','原生检查点不存在')
    work=output_root/'engineering';work.mkdir(exist_ok=True)
    document=App.openDocument(str(source))
    extra_files={}
    try:
        selected=next((shape for obj,shape in component_shapes(document) if obj.Name==name),None)
        if selected is None or len(selected.Solids)!=1 or not selected.isValid():
            raise EngineeringError('engineering_component_invalid','当前工程计算需要一个有效的单实体部件')
        if params['kind']=='linear_static':
            report,data=linear_static(selected,params,work)
        elif params['kind']=='contour_milling':
            from freecad_cam import contour_milling
            report,data,program=contour_milling(selected,params,work)
            destination=output_root/program.name
            shutil.copyfile(program,destination)
            extra_files['cam_program']=str(destination)
        else:
            raise EngineeringError('engineering_operation_unsupported','此工程计算类型尚未实现')
    finally:
        App.closeDocument(document.Name)
    report.update(component_name=name,source_fcstd_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),freecad_version='.'.join(App.Version()[:3]))
    report_path=output_root/'engineering-report.json';field_path=output_root/'engineering-field.json'
    report_path.write_text(json.dumps(report,ensure_ascii=False,allow_nan=False))
    field_path.write_text(json.dumps(data,allow_nan=False,separators=(',',':')))
    bundle=output_root/'engineering-evidence.zip'
    evidence=[p for p in work.iterdir() if p.is_file() and p.suffix in {'.step','.geo','.inp','.frd','.dat','.sta','.log','.nc','.json'}]
    if sum(p.stat().st_size for p in evidence)>100*1024*1024:
        raise EngineeringError('engineering_output_budget','工程证据超过文件预算')
    with zipfile.ZipFile(bundle,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in [report_path,field_path,*evidence]: archive.write(path,path.name)
    return {'schema_version':'freecad-engineering-result.v1','status':'succeeded',
        'files':{'engineering_report':str(report_path),'engineering_field':str(field_path),'engineering_bundle':str(bundle),**extra_files},
        'component_name':name,'kind':params['kind']}
