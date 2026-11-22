"""Runs only inside an offline, resource-limited CAD container."""
import ast
import hashlib
import json
import math
import sys
import traceback
from pathlib import Path
import cadquery as cq
import numpy as np

def approved_tree(code, reference=False):
    tree=ast.parse(code)
    forbidden={'eval','exec','open','compile','__import__','getattr','setattr','delattr','globals','locals','breakpoint','input'}
    for n in ast.walk(tree):
        if isinstance(n,(ast.Import,ast.ImportFrom)):
            modules=[a.name for a in n.names] if isinstance(n,ast.Import) else [n.module or '']
            if any(m.split('.')[0] not in {'cadquery','math','numpy'} for m in modules):
                raise ValueError('unapproved import: '+str(modules))
        if isinstance(n,ast.Name) and (n.id in forbidden or n.id.startswith('__')): raise ValueError('unapproved name '+n.id)
        if isinstance(n,ast.Attribute) and n.attr.startswith('__'): raise ValueError('dunder access')
    if reference:
        # Preserve construction; intercept only the final export to identify the result.
        exports=[]
        class Exports(ast.NodeTransformer):
            def visit_Expr(self,node):
                c=node.value
                if isinstance(c,ast.Call) and isinstance(c.func,ast.Attribute) and c.func.attr=='export':
                    exports.append(c.args[0])
                    return ast.copy_location(ast.Assign(targets=[ast.Name(id='_captured_result',ctx=ast.Store())], value=c.args[0]),node)
                return self.generic_visit(node)
        tree=Exports().visit(tree)
        if not exports: raise ValueError('no reference export')
    return ast.fix_missing_locations(tree)

def tests(model, checks):
    rows=[]
    for item in checks:
        calls=[]
        def check(condition,pass_msg,fail_msg):
            calls.append(bool(condition))
            if not condition: raise AssertionError(str(fail_msg))
        try:
            ns={'cq':cq,'math':math,'np':np,'numpy':np,'final_result':model,'check':check}
            exec(compile(approved_tree(item['cadtest_code']),'<upstream-check>','exec'),ns)
            if not calls: raise ValueError('test did not call check')
            rows.append({'id':item['cadtest_id'],'requirement_id':item['requirement_id'],'status':'pass'})
        except AssertionError as exc: rows.append({'id':item['cadtest_id'],'requirement_id':item['requirement_id'],'status':'fail','message':str(exc)})
        except Exception as exc: rows.append({'id':item['cadtest_id'],'requirement_id':item['requirement_id'],'status':'error','message':type(exc).__name__+': '+str(exc)})
    for row,item in zip(rows,checks):row['role']=item.get('scoring_role','required')
    required=[r for r in rows if r['role']=='required']
    return {'passed':bool(required) and all(x['status']=='pass' for x in required),'checks':rows}

def main():
    case=Path(sys.argv[1]); out=Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)
    try:
        ns={'cq':cq,'math':math,'np':np}
        exec(compile(approved_tree((case/'reference.py').read_text(),True),'<upstream-reference>','exec'),ns)
        raw=ns['_captured_result']; model=raw if isinstance(raw,cq.Workplane) else cq.Workplane().newObject([raw])
        shape=model.val()
        if not shape.isValid() or not shape.Solids() or shape.Volume()<=0:raise ValueError('invalid reference solid')
        cq.exporters.export(model,str(out/'reference.step'))
        cq.exporters.export(model,str(out/'reference.stl'))
        reopened=cq.importers.importStep(str(out/'reference.step'))
        checks=json.loads((case/'checks.json').read_text())
        direct=tests(model,checks); roundtrip=tests(reopened,checks)
        b=shape.BoundingBox(); center=shape.Center()
        candidates={'scale_120_percent':shape.scale(1.2),
            'replace_with_sphere':cq.Solid.makeSphere((shape.Volume()*3/(4*math.pi))**(1/3),center),
            'truncate_top_20_percent':shape.cut(cq.Solid.makeBox(b.xlen*1.4,b.ylen*1.4,b.zlen*.3,cq.Vector(b.xmin-b.xlen*.2,b.ymin-b.ylen*.2,b.zmax-b.zlen*.2)))}
        mutants={}
        for name,s in candidates.items():
            cq.exporters.export(s,str(out/(name+'.step')))
            verdict=tests(cq.importers.importStep(str(out/(name+'.step'))),checks)
            mutants[name]={'valid':s.isValid(),'volume':s.Volume(),'killed':not verdict['passed'] and any(r['status']=='fail' for r in verdict['checks']),**verdict}
        result={'source_id':case.name,'direct':direct,'roundtrip':roundtrip,'mutants':mutants,
            'volume':shape.Volume(),'area':shape.Area(),'solids':len(shape.Solids()),'faces':len(shape.Faces()),
            'bbox':[b.xlen,b.ylen,b.zlen],
            'eligible':direct['passed'] and roundtrip['passed'] and all(m['killed'] for m in mutants.values())}
    except Exception as exc:result={'source_id':case.name,'eligible':False,'error':type(exc).__name__+': '+str(exc),'traceback':traceback.format_exc()}
    (out/'validation.json').write_text(json.dumps(result,indent=2))
    print(case.name,result['eligible'],flush=True)

if __name__=='__main__':main()
