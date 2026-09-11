"""Revision-bound native CAD/BOM/engineering release package."""
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

import Assembly  # Registers the real native Assembly::BomObject.
import FreeCAD as App
import MeshPart
import Part

from freecad_bom import _cell, spreadsheet_literal
from freecad_engineering import EngineeringError
from freecad_scene import component_shapes

MAX_RELEASE_BYTES=128*1024*1024


def _csv_value(value):
    return spreadsheet_literal(value)


def _file(path,role):
    return {'path':path.name,'role':role,'size_bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def _native_bom(components,directory,source):
    document=App.newDocument('ReleaseBOM')
    inventory=[]
    try:
        for index,(obj,shape) in enumerate(components):
            definition=obj.getLinkedObject(True) if obj.TypeId=='App::Link' else obj
            container=document.addObject('App::Part',f'Component{index}')
            container.Label=spreadsheet_literal(obj.Label)
            for prop,value in [('KernelName',obj.Name),('DefinitionName',definition.Name)]:
                container.addProperty('App::PropertyString',prop,'Release');setattr(container,prop,value)
            geometry=document.addObject('Part::Feature',f'Geometry{index}');geometry.Shape=shape.copy();container.addObject(geometry)
            inventory.append({'kernel_name':obj.Name,'definition_name':definition.Name,'label':obj.Label,
                'volume_mm3':shape.Volume,'solid_count':len(shape.Solids),
                'placement':list(shape.Placement.toMatrix().A),'visible':bool(getattr(obj,'Visibility',True))})
        document.recompute();document.saveAs(str(directory/'bom-source.FCStd'))
        bom=document.addObject('Assembly::BomObject','ReleaseBOMTable')
        columns=['Index','Name','Quantity','File Name','.KernelName','.DefinitionName']
        bom.columnsNames=columns;bom.detailParts=False;bom.onlyParts=True;document.recompute()
        actual=[_cell(bom,i,0).lstrip('.') for i in range(len(columns))]
        if actual!=[c.lstrip('.') for c in columns]:
            raise EngineeringError('release_bom_columns','原生 BOM 未返回请求列')
        rows=[]
        for i in range(1,len(components)+2):
            values=[_cell(bom,j,i) for j in range(len(columns))]
            if not any(values):break
            try:quantity=int(values[2])
            except ValueError as exc:raise EngineeringError('release_bom_quantity','原生 BOM 数量无效') from exc
            original_label=next((item['label'] for item in inventory if item['kernel_name']==values[4]),None)
            if original_label is None or values[1]!=original_label.lstrip("'"):
                raise EngineeringError('release_bom_label','原生 BOM 名称与组件不一致')
            rows.append({'index':values[0],'name':original_label,'quantity':quantity,'file_name':Path(values[3]).name,
                'kernel_name':values[4],'definition_name':values[5]})
        if sum(r['quantity'] for r in rows)!=len(components) or {r['kernel_name'] for r in rows}!={i['kernel_name'] for i in inventory}:
            raise EngineeringError('release_bom_inventory','原生 BOM 与 FCStd 实体/实例清单不一致')
        payload={'schema_version':'cad-release-bom.v1','source':source,
            'generator':{'native_type':str(bom.TypeId),'freecad_version':'.'.join(App.Version()[:3])},
            'columns':actual,'rows':rows,'instances':inventory,
            'instance_count':len(inventory),'definition_count':len({i['definition_name'] for i in inventory}),
            'inclusion_policy':'all_final_solid_components_including_hidden'}
        json_path=directory/'bom.json';csv_path=directory/'bom.csv'
        json_path.write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False))
        with csv_path.open('w',encoding='utf-8',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(actual)
            for row in rows:writer.writerow([_csv_value(row[k]) for k in ('index','name','quantity','file_name','kernel_name','definition_name')])
        document.save()
        return payload,json_path,csv_path
    finally:
        App.closeDocument(document.Name)


def run_release(task,input_root,output_root):
    params=task['params'];source=params['source'];base_name=task.get('inputs',{}).get('base')
    if not isinstance(base_name,str) or Path(base_name).name!=base_name:
        raise EngineeringError('release_source_missing','发布需要原生 FCStd 输入')
    base=input_root/base_name
    if base.suffix.lower()!='.fcstd' or not base.is_file() or base.is_symlink():
        raise EngineeringError('release_source_missing','发布来源 FCStd 不存在')
    digest=hashlib.sha256(base.read_bytes()).hexdigest()
    if digest!=source['fcstd_sha256']:
        raise EngineeringError('release_source_integrity','发布来源 FCStd 哈希不一致')
    directory=output_root/'release';directory.mkdir(exist_ok=True)
    shutil.copyfile(base,directory/'design.FCStd')
    document=App.openDocument(str(base))
    try:
        components=component_shapes(document)
        if not components or len(components)>1000 or any(not s.isValid() or s.Volume<=0 for _,s in components):
            raise EngineeringError('release_geometry_invalid','发布需要 1 至 1000 个有效实体部件/实例')
        combined=Part.makeCompound([s for _,s in components])
        combined.exportStep(str(directory/'design.step'))
        mesh=MeshPart.meshFromShape(Shape=combined,LinearDeflection=0.15,AngularDeflection=0.35,Relative=False)
        if not 1<=mesh.CountFacets<=2000000:
            raise EngineeringError('release_mesh_budget','发布 STL 网格为空或超过预算')
        mesh.write(str(directory/'design.stl'))
        bom,bom_json,bom_csv=_native_bom(components,directory,source)
        volume=sum(s.Volume for _,s in components)
    finally:
        App.closeDocument(document.Name)
    files=[_file(directory/name,role) for name,role in [('design.FCStd','fcstd'),('design.step','step'),('design.stl','stl'),
        ('bom.json','bom_json'),('bom.csv','bom_csv'),('bom-source.FCStd','bom_source')]]
    engineering=[]
    for index,reference in enumerate(params.get('engineering_artifacts',[])):
        input_name=task['inputs'].get(f'evidence_{index}')
        if not isinstance(input_name,str) or Path(input_name).name!=input_name:
            raise EngineeringError('release_evidence_missing','发布缺少已声明的工程工件')
        path=input_root/input_name
        if not path.is_file() or path.is_symlink() or path.stat().st_size!=reference['size_bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=reference['sha256']:
            raise EngineeringError('release_evidence_integrity','发布工程工件完整性校验失败')
        name=reference['package_filename']
        if not isinstance(name,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,239}',name) is None or name=='manifest.json' or any(name==f['path'] for f in files):
            raise EngineeringError('release_evidence_name','发布工件文件名不安全或重复')
        if reference['source_revision_id']!=source['revision_id']:
            raise EngineeringError('release_evidence_revision','不能把其他修订的工程证据打入当前发布')
        if reference['artifact_kind']=='engineering_report':
            measured=json.loads(path.read_text())
            if measured.get('source_fcstd_sha256')!=digest:
                raise EngineeringError('release_evidence_revision','工程报告属于不同的原生模型')
        shutil.copyfile(path,directory/name)
        files.append(_file(directory/name,reference['artifact_kind']));engineering.append(reference)
    manifest={'schema_version':'cad-engineering-release.v1','release_name':params['release_name'],'source':source,
        'files':files,'engineering_artifacts':engineering,'annotations':params.get('annotations',[]),
        'bom':{'native_type':bom['generator']['native_type'],'instance_count':bom['instance_count'],'definition_count':bom['definition_count']},
        'units':'mm','total_component_volume_mm3':volume,'mesh_triangles':mesh.CountFacets,
        'scope':'原生 CAD、全部最终实体/实例的 BOM、STEP/STL 与同一修订的已选工程证据；此发布不代表实机制造验收。'}
    report_path=output_root/'engineering-report.json'
    report_path.write_text(json.dumps({**manifest,'kind':'release_package','source_fcstd_sha256':digest},ensure_ascii=False,allow_nan=False))
    manifest_path=directory/'manifest.json';manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,allow_nan=False))
    if sum((directory/f['path']).stat().st_size for f in files)+manifest_path.stat().st_size>MAX_RELEASE_BYTES:
        raise EngineeringError('release_output_budget','发布内容超过文件预算')
    archive=output_root/'engineering-release.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in ['manifest.json',*(f['path'] for f in files)]:bundle.write(directory/name,name)
    outputs={'engineering_report':report_path,'engineering_bundle':archive,'release_manifest':manifest_path,
        'release_bom_json':bom_json,'release_bom_csv':bom_csv,'release_step':directory/'design.step','release_stl':directory/'design.stl'}
    # The capability wrapper only exposes files directly inside output_root.
    for key,path in list(outputs.items()):
        if path.parent!=output_root:
            target=output_root/path.name;shutil.copyfile(path,target);outputs[key]=target
    if hashlib.sha256(base.read_bytes()).hexdigest()!=digest:
        raise EngineeringError('release_source_changed','发布过程改变了来源 FCStd')
    return {'schema_version':'freecad-engineering-result.v1','status':'succeeded',
        'kind':'release_package','files':{k:str(v) for k,v in outputs.items()}}
