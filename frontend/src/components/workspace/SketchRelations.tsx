import { useRef, useState } from 'react';
import { authFetch } from '../../auth';
import { API_BASE, EngineeringApiError, readJson } from '../../services/clients/http';
import { useFeatureLease } from '../../hooks/useFeatureLease';
import { useDraftGuard } from '../../hooks/useDraftGuard';
import { useDraftHistory } from '../../hooks/useDraftHistory';
import type { CloudDocument, SemanticFeature } from '../../types/document';
import type { SketchDetails } from '../../types/sketch';

interface Relation {sketch:string;kind:'horizontal'|'vertical'|'coincident'|'equal';first:{geometry_index:number;point_position?:number};second?:{geometry_index:number;point_position?:number}}
interface Change {action:'add'|'delete'|'replace';logical_id:string;constraint?:Relation}
export default function SketchRelations({document,feature,details,onSubmitted}: {
  document:CloudDocument;feature:SemanticFeature;details:SketchDetails;onSubmitted?:(id:string,base:CloudDocument)=>void;
}) {
  const [base]=useState(document),[kind,setKind]=useState<Relation['kind']>('horizontal');
  const [first,setFirst]=useState(''),[second,setSecond]=useState(''),[replacement,setReplacement]=useState('');
  const [key,setKey]=useState(()=>crypto.randomUUID()),[token,setToken]=useState<string|null>(null);
  const [pending,setPending]=useState(false),[uncertain,setUncertain]=useState(false),[submitted,setSubmitted]=useState(false),[error,setError]=useState('');
  const history=useDraftHistory<Change[]>([]), busy=useRef(false),lease=useFeatureLease(document,feature.id);
  const stale=base.head_revision_id!==document.head_revision_id || base.state_version!==document.state_version;
  const editable=document.can_edit && !!onSubmitted && !!feature.sketch_constraints_sha256 && !details.omitted_constraints && !details.omitted_geometry;
  const locked=pending || uncertain || submitted || stale || !editable;
  useDraftGuard(history.value.length>0 && !submitted || pending,'草图关系草稿',()=>{history.reset([]);setKey(crypto.randomUUID());});
  const choices=details.geometry.filter(g=>kind==='equal' ? ['Part::GeomLineSegment','Part::GeomCircle'].includes(g.type) : g.type==='Part::GeomLineSegment')
    .flatMap(g=>kind==='coincident' ? [1,2].map(point=>({value:`${g.index}:${point}`,label:`线 ${g.index} · ${point===1?'起点':'终点'}`})) : [{value:String(g.index),label:`几何 ${g.index} · ${g.type.includes('Circle')?'圆':'线'}`}]);
  const reference=(value:string)=>{const [geometry_index,point_position]=value.split(':').map(Number);return {geometry_index,...(point_position ? {point_position}: {})};};
  const add=()=>{
    if (locked || !first || (['coincident','equal'].includes(kind) && (!second || first===second))) return;
    const logical_id=replacement || `relation_${crypto.randomUUID().replaceAll('-','')}`;
    history.set(rows=>[...rows.filter(r=>r.logical_id!==logical_id),{action:replacement?'replace':'add',logical_id,
      constraint:{sketch:feature.kernel_name,kind,first:reference(first),...(['coincident','equal'].includes(kind)?{second:reference(second)}:{})}}]);
    setKey(crypto.randomUUID());setReplacement('');
  };
  const submit=async()=>{
    if (busy.current || !history.value.length || submitted || !editable || stale && !uncertain) return;
    busy.current=true;setPending(true);setError('');
    try {
      const leaseToken=token || await lease.ensure();setToken(leaseToken);
      const receipt=await readJson<{workflow_run_id:string}>(await authFetch(`${API_BASE}/api/documents/${base.document_id}/operations`,{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'native.update',
          expected_base_revision_id:base.head_revision_id,expected_state_version:base.state_version,idempotency_key:key,lease_token:leaseToken,
          objective:`修改草图 ${feature.label} 的受保护几何关系`,modification:{expected_state_sha256:base.parameter_state_sha256,
            native_edits:[{action:'sketch.patch_relations',args:{sketch:feature.kernel_name,expected_constraints_sha256:feature.sketch_constraints_sha256,changes:history.value}}]}})}),'关系修改提交失败');
      setSubmitted(true);setUncertain(false);onSubmitted?.(receipt.workflow_run_id,base);void lease.release().catch(()=>{});
    } catch(e) {
      const definitive=e instanceof EngineeringApiError && e.httpStatus!==undefined && e.httpStatus<500;
      if (definitive) setToken(null);setUncertain(!definitive);setError(e instanceof Error?e.message:'关系修改失败');
    } finally {busy.current=false;setPending(false);}
  };
  return <details className="my-3" aria-label="受保护关系编辑"><summary>几何关系增删与替换</summary>
    <p className="my-2 type-caption">仅开放水平、垂直、端点连接和相等关系。后端核对完整基线、关系证明、原生求解和参数联动；来源未知、必要尺寸、表达式与不支持关系继续保护。最终结果仍需审核。</p>
    {!editable?<p role="status">此检查点尚不具备完整关系编辑证据。</p>:null}
    <div className="grid gap-2 sm:grid-cols-3"><label>关系<select className="ww-field w-full" value={kind} disabled={locked} onChange={e=>{setKind(e.target.value as Relation['kind']);setFirst('');setSecond('');}}><option value="horizontal">水平</option><option value="vertical">垂直</option><option value="coincident">端点连接</option><option value="equal">相等</option></select></label>
      {[0,...(['coincident','equal'].includes(kind)?[1]:[])].map(i=><label key={i}>几何 {i+1}<select className="ww-field w-full" disabled={locked} value={i?second:first} onChange={e=>(i?setSecond:setFirst)(e.target.value)}><option value="">请选择</option>{choices.map(c=><option key={c.value} value={c.value}>{c.label}</option>)}</select></label>)}</div>
    <button type="button" className="workspace-button my-2" disabled={locked || !first || ['coincident','equal'].includes(kind) && (!second || first===second)} onClick={add}>{replacement?'加入关系替换草稿':'加入关系草稿'}</button>
    {details.constraints.filter(c=>['user_relation','typed_operation'].includes(c.origin || '') && c.logical_id && ['Horizontal','Vertical','Coincident','Equal'].includes(c.type)).map(c=><div key={c.logical_id} className="my-2 flex flex-wrap gap-2 type-caption"><span>{c.type} · {c.logical_id}</span><button type="button" className="workspace-button" disabled={locked} onClick={()=>setReplacement(c.logical_id!)}>替换此关系</button><button type="button" className="workspace-button" disabled={locked} onClick={()=>{history.set(rows=>[...rows.filter(r=>r.logical_id!==c.logical_id),{action:'delete',logical_id:c.logical_id!}]);setKey(crypto.randomUUID());}}>加入删除草稿</button></div>)}
    <p role="status" className="type-caption">{submitted?'关系修改已受理，等待候选审核。':`${history.value.length} 项关系草稿；尚未改变已保存模型。`}</p>
    <div className="my-2 flex flex-wrap gap-2"><button type="button" className="workspace-button" disabled={locked || !history.canUndo} onClick={()=>{history.undo();setKey(crypto.randomUUID());}}>撤销关系草稿</button><button type="button" className="workspace-button" disabled={locked || !history.canRedo} onClick={()=>{history.redo();setKey(crypto.randomUUID());}}>重做关系草稿</button>
      <button type="button" className="workspace-button" disabled={pending || submitted || !editable || !history.value.length || stale && !uncertain} onClick={()=>void submit()}>{pending?'正在提交':uncertain?'核对并重试原关系请求':'提交关系修改'}</button></div>
    {error?<p role="alert" className="text-red-700">{error}</p>:null}
  </details>;
}
