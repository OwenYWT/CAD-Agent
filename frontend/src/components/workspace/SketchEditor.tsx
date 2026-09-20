import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import type { CloudDocument, SemanticFeature } from "../../types/document";
import type { SketchConstraint, SketchDetails, SketchDimensionType } from "../../types/sketch";
import { dimensionLabels, editableDimension, previewSketch, sketchBounds, validDimension } from "../../adapters/sketchPreview";
import { readDocumentSketch, updateSketchDimensions } from "../../services/clients/documents";
import { EngineeringApiError } from "../../services/clients/http";
import { useFeatureLease } from "../../hooks/useFeatureLease";
import { useDraftGuard } from "../../hooks/useDraftGuard";
import { guardDraft } from "../../stores/draftGuard";

export default function SketchEditor({document,feature,onSubmitted}: {
  document: CloudDocument; feature: SemanticFeature; onSubmitted?: (taskId:string,document:CloudDocument)=>void;
}) {
  const [base,setBase]=useState(document);
  const [details,setDetails]=useState<SketchDetails | null>(null);
  const [values,setValues]=useState<Record<number,string>>({});
  const [key,setKey]=useState(()=>crypto.randomUUID());
  const [pending,setPending]=useState(false);
  const [error,setError]=useState("");
  const [submittedId,setSubmittedId]=useState<string | null>(null);
  const [attemptToken,setAttemptToken]=useState<string | null>(null);
  const [uncertain,setUncertain]=useState(false);
  const [fitted,setFitted]=useState<[number,number,number,number] | null>(null);
  const drag=useRef<{geometry:number;kind:"radius"|"center";pointer:number} | null>(null);
  const lease=useFeatureLease(document,feature.id);
  useEffect(()=>{
    const controller=new AbortController();
    void readDocumentSketch(base,feature,controller.signal).then(result=>{if (!controller.signal.aborted) {setDetails(result);setError("");}}).catch((e: unknown)=>{
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "草图加载失败");
    });
    return ()=>controller.abort();
  },[base,feature]);
  const geometry=useMemo(()=>previewSketch(details?.geometry || [],details?.constraints || [],values),[details,values]);
  const bounds=fitted || sketchBounds(details?.geometry || []), handleSize=bounds[2]*0.022;
  const stale=base.head_revision_id!==document.head_revision_id || base.state_version!==document.state_version;
  const canEdit=document.can_edit && !!onSubmitted && !pending && !stale && !submittedId && !uncertain;
  const constraints=details?.constraints || [];
  const updates=constraints.filter(c=>editableDimension(c) && values[c.index]!==undefined && Number(values[c.index])!==c.value)
    .map(c=>({constraint_index:c.index,expected_type:c.type as SketchDimensionType,value_mm:Number(values[c.index])}));
  const valid=updates.length>0 && updates.length<=20 && constraints.every(c=>values[c.index]===undefined ||
    (!!values[c.index].trim() && validDimension(c,Number(values[c.index]))));
  const change=(updates:Record<number,string>)=>{setValues(v=>({...v,...updates}));setKey(crypto.randomUUID());};
  const direct=(index:number,type:string)=>constraints.find(c=>c.first===index && c.type===type && editableDimension(c) &&
    (type==="Radius" || type==="Diameter" || (c.first_position===3 && c.second===-2000)));
  const start=(event:ReactPointerEvent<SVGCircleElement>,index:number,kind:"radius"|"center")=>{
    if (!canEdit) return;
    event.preventDefault();event.stopPropagation();
    drag.current={geometry:index,kind,pointer:event.pointerId};
    event.currentTarget.ownerSVGElement!.setPointerCapture(event.pointerId);
    void lease.ensure().catch(()=>{});
  };
  const move=(event:ReactPointerEvent<SVGSVGElement>)=>{
    if (!drag.current || !canEdit) return;
    const item=geometry.find(g=>g.index===drag.current!.geometry);
    const matrix=event.currentTarget.getScreenCTM();
    if (!item?.center || !matrix) return;
    const point=new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
    if (drag.current.kind==="radius") {
      const c=direct(item.index,"Radius") || direct(item.index,"Diameter");
      if (c) change({[c.index]:String(Number((Math.max(0.01,Math.hypot(point.x-item.center[0],-point.y-item.center[1]))*(c.type==="Diameter" ? 2 : 1)).toFixed(3)))});
    } else {
      const x=direct(item.index,"DistanceX"),y=direct(item.index,"DistanceY");
      if (x && y) change({[x.index]:String(Number(point.x.toFixed(3))),[y.index]:String(Number((-point.y).toFixed(3)))});
    }
  };
  const stop=(event:ReactPointerEvent<SVGSVGElement>)=>{
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    drag.current=null;
  };
  const submit=async()=>{
    if (!valid || submittedId || (stale && !uncertain) || !document.can_edit) return;
    setPending(true);setError("");
    try {
      const token=attemptToken || await lease.ensure();
      setAttemptToken(token);
      const result=await updateSketchDimensions(base,feature,updates,key,token);
      setSubmittedId(result.workflow_run_id);setUncertain(false);
      onSubmitted?.(result.workflow_run_id,base);
      void lease.release().catch(()=>{});
    } catch(e) {
      const definitive=e instanceof EngineeringApiError && e.httpStatus !== undefined && e.httpStatus < 500;
      if (definitive) setAttemptToken(null);
      setUncertain(!definitive);setError(e instanceof Error ? e.message : "草图修改失败");
    }
    finally {setPending(false);}
  };
  const refresh=()=>{setValues({});setDetails(null);setFitted(null);setKey(crypto.randomUUID());setBase(document);setSubmittedId(null);setAttemptToken(null);setUncertain(false);void lease.release().catch(()=>{});};
  useDraftGuard((Object.keys(values).length > 0 && !submittedId) || pending, `${feature.label} 草图约束`, refresh);
  const dimensionStep=(c:SketchConstraint,delta:number)=>{
    const value=Number(values[c.index] ?? c.value)+delta;
    if (canEdit && validDimension(c,value)) change({[c.index]:String(Number(value.toFixed(3)))});
  };
  const unsupported=geometry.filter(g=>!['Part::GeomCircle','Part::GeomLineSegment'].includes(g.type)).length;
  return <section className="ww-inspector-section" aria-label="草图编辑" data-testid="sketch-editor">
    <h3>{feature.label} · 草图尺寸</h3>
    <p className="type-caption">蓝色控制点可拖动圆心和半径，也可输入尺寸。预览只更新本地草稿，提交后由云端求解并审核。</p>
    {!details ? <p role="status" className="mt-2 type-caption">正在读取草图检查点…</p> : <>
      <svg role="group" aria-label="草图本地预览" data-testid="sketch-preview" data-revision={base.head_revision_id}
        className="my-3 h-64 w-full touch-none rounded border border-[var(--line)] bg-[var(--surface)]" viewBox={bounds.join(' ')}
        onPointerMove={move} onPointerUp={stop} onPointerCancel={stop}>
        <line x1={bounds[0]} x2={bounds[0]+bounds[2]} y1={0} y2={0} stroke="#a8b1b9" strokeWidth={bounds[2]*0.002} />
        <line x1={0} x2={0} y1={bounds[1]} y2={bounds[1]+bounds[3]} stroke="#a8b1b9" strokeWidth={bounds[2]*0.002} />
        {geometry.map(item=>{
          if (item.type==='Part::GeomLineSegment' && item.start && item.end) return <line key={item.index} x1={item.start[0]} y1={-item.start[1]} x2={item.end[0]} y2={-item.end[1]}
            stroke="#263c50" strokeWidth={bounds[2]*0.006} strokeDasharray={item.construction ? `${handleSize} ${handleSize}` : undefined} />;
          if (item.type!=='Part::GeomCircle' || !item.center || !item.radius_mm) return null;
          const [x,y]=item.center, radius=direct(item.index,'Radius') || direct(item.index,'Diameter');
          return <g key={item.index}>
            <circle data-testid={`sketch-circle-${item.index}`} cx={x} cy={-y} r={item.radius_mm} fill="none" stroke="#263c50" strokeWidth={bounds[2]*0.006}
              strokeDasharray={item.construction ? `${handleSize} ${handleSize}` : undefined} />
            {canEdit && radius ? <circle data-testid={`sketch-radius-handle-${item.index}`} role="slider" tabIndex={0} aria-label={`圆 ${item.index} 半径`}
              aria-valuenow={item.radius_mm} aria-valuemin={0.01} aria-valuemax={1000000} cx={x+item.radius_mm} cy={-y} r={handleSize} fill="#2563eb" className="cursor-ew-resize"
              onPointerDown={e=>start(e,item.index,'radius')} onKeyDown={e=>{if (['ArrowLeft','ArrowDown','ArrowRight','ArrowUp'].includes(e.key)) {e.preventDefault();dimensionStep(radius,['ArrowLeft','ArrowDown'].includes(e.key) ? -0.1 : 0.1);}}} /> : null}
            {canEdit && direct(item.index,'DistanceX') && direct(item.index,'DistanceY') ? <circle data-testid={`sketch-center-handle-${item.index}`} cx={x} cy={-y} r={handleSize} fill="#2563eb" className="cursor-move"
              onPointerDown={e=>start(e,item.index,'center')}><title>拖动圆心；键盘用户可编辑下方 X/Y 距离</title></circle> : null}
          </g>;
        })}
      </svg>
      <button className="workspace-button" type="button" onClick={()=>setFitted(sketchBounds(geometry))}>适配预览</button>
      <p className="my-2 type-caption text-[var(--muted)]">本地预览支持圆和线段的直接尺寸；关联约束及其他几何以云端求解结果为准。</p>
      {unsupported || details.omitted_geometry || details.omitted_constraints ? <p role="status" className="type-caption">未绘制 {unsupported+details.omitted_geometry} 项几何；未记录 {details.omitted_constraints} 项约束。</p> : null}
      {constraints.map(c=><label key={c.index} className="my-2 flex items-center gap-2 type-caption"><span className="min-w-0 flex-1">
        {c.name || dimensionLabels[c.type as SketchDimensionType] || c.type} · #{c.index}{c.driving===false ? "（测量）" : ""}</span>
        {editableDimension(c) ? <><input aria-label={`草图约束 ${c.index} ${c.type}`} className="w-24 rounded border border-[var(--line)] p-2" type="number" step="any"
          value={values[c.index] ?? c.value} disabled={!canEdit} onFocus={()=>{void lease.ensure().catch(()=>{});}} onChange={e=>change({[c.index]:e.target.value})} /><span>mm</span></>
          : <span>{c.value}</span>}
      </label>)}
      {document.can_edit && onSubmitted ? <button className="workspace-button" type="button" disabled={(!canEdit && !uncertain) || pending || !valid || !!submittedId} onClick={()=>void submit()}>{uncertain ? "核对并重试同一草图请求" : submittedId ? "已提交候选计算" : "提交草图约束"}</button> : null}
      {updates.length ? <p role="status" className="mt-2 type-caption">{submittedId ? `请求 ${submittedId.slice(0,8)} 已受理；保留本次提交值，等待候选审核。` : `${updates.length} 项草稿尺寸尚未更新已提交模型。`} 基线 v{base.state_version} · {base.head_revision_id.slice(0,8)}</p> : null}
      {submittedId ? <button className="workspace-button mt-2" type="button" onClick={refresh}>结束本次草图查看</button> : null}
    </>}
    {stale ? <p role="status" className="mt-2 type-caption">文档已有新版本，请读取最新草图；当前草稿不会自动覆盖新约束。
      <button className="workspace-button mt-2" type="button" onClick={()=>guardDraft(refresh)}>读取最新草图并清除草稿</button></p> : null}
    {lease.error ? <p role="alert" className="mt-2 type-caption text-red-700">{lease.error}</p> : null}
    {error ? <p role="alert" className="mt-2 type-caption text-red-700">{error}</p> : null}
  </section>;
}
