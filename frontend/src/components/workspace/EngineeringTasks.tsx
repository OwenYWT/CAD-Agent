import { cancelEngineeringTask } from "../../services/clients/tasks";
import { downloadEngineeringArtifact } from "../../services/clients/downloads";
import { useEffect, useState } from 'react';
import type { CloudDocument } from '../../types/document';
import type { BoundaryPlane, EngineeringTaskSummary, EngineeringResult, EngineeringField, LinearStaticTask, CamToolpath } from '../../types/engineeringTask';
import { listEngineeringTasks, submitEngineeringTask, readEngineeringResult, readEngineeringField, readCamToolpath } from '../../services/clients/engineering';
import EngineeringFieldViewer from '../viewer/EngineeringFieldViewer';
import CamToolpathViewer from '../viewer/CamToolpathViewer';
import ContourMillingForm from './ContourMillingForm';
import { engineeringSourceLabel } from '../../adapters/documentView';

const active = (status: string) => ['pending','planning','running','cancelling'].includes(status);
const statuses: Record<string,string> = {pending:'排队中',planning:'准备计算',running:'正在计算',cancelling:'正在取消',cancelled:'已取消',failed:'计算失败',timed_out:'计算超时',succeeded:'计算完成'};
const inputClass = 'mt-1 w-full rounded border border-[var(--line)] bg-white p-2';

function Plane({label,value,onChange}: {label:string; value:BoundaryPlane; onChange:(v:BoundaryPlane)=>void}) {
  return <label className="block type-caption">{label}<select className={inputClass} aria-label={label} value={`${value.axis}:${value.side}`}
    onChange={e=>{const [axis,side]=e.target.value.split(':');onChange({axis:axis as BoundaryPlane['axis'],side:side as BoundaryPlane['side']});}}>
    {(['x','y','z'] as const).flatMap(axis=>(['min','max'] as const).map(side=><option key={axis+side} value={`${axis}:${side}`}>{axis.toUpperCase()} {side==='min' ? '最小' : '最大'}平面</option>))}
  </select></label>;
}

function Result({documentId,taskId,viewedRevision,headRevision,canExport}: {documentId:string;taskId:string;viewedRevision:string;headRevision:string;canExport:boolean}) {
  const [loaded,setLoaded]=useState<{result:EngineeringResult;field:EngineeringField | CamToolpath} | null>(null);
  const [error,setError]=useState('');
  useEffect(()=>{
    const controller=new AbortController();
    void readEngineeringResult(documentId,taskId,controller.signal).then(async result=>{
      const field=await (result.report.kind==='contour_milling' ? readCamToolpath : readEngineeringField)(result.artifacts.engineering_field,controller.signal);
      if (!controller.signal.aborted) setLoaded({result,field});
    }).catch((e:unknown)=>{if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '结果加载失败');});
    return ()=>controller.abort();
  },[documentId,taskId]);
  if (!loaded) return <p role={error ? 'alert' : 'status'} className="my-2 type-caption">{error || '正在校验并读取计算结果…'}</p>;
  const {result,field}=loaded,report=result.report;
  const sourceLabel = `来源：v${result.source_state_version} · ${result.source_revision_id.slice(0,8)} · ${report.component_name}（${engineeringSourceLabel(result.source_revision_id,viewedRevision,headRevision)}）`;
  const download=(kind:'engineering_bundle' | 'cam_program')=>{
    const ref=result.artifacts[kind];
    if (!ref) {setError('计算结果缺少该文件');return;}
    void downloadEngineeringArtifact(ref.url,ref.filename).catch((e:unknown)=>setError(e instanceof Error ? e.message : '文件下载失败'));
  };
  if (report.kind==='contour_milling' && field.schema_version==='cad-cam-toolpath.v1') return <div className="mt-3" data-testid="cam-result" data-workflow={taskId} data-revision={result.source_revision_id}>
    <p className="type-caption" data-testid="engineering-source-version">{sourceLabel}</p>
    <CamToolpathViewer field={field} cuttingLength={report.tool.cutting_length_mm}/>
    <dl className="my-3 grid grid-cols-2 gap-1 type-caption"><dt>轴向层数</dt><dd>{report.passes}</dd><dt>路径段数</dt><dd>{report.segments}</dd>
      <dt>目标最小径向间隙</dt><dd>{report.minimum_target_clearance_mm.toPrecision(5)} mm</dd><dt>进给路径长度</dt><dd>{report.feed_path_length_mm.toFixed(2)} mm</dd>
      <dt>进给时间</dt><dd>{report.feed_time_seconds.toFixed(1)} s（不含快移）</dd><dt>本工序未加工内环</dt><dd>{report.internal_loops_not_machined}</dd></dl>
    <p className="type-caption">{report.scope}</p>
    <details className="my-2 type-caption"><summary className="cursor-pointer">刀具与坐标设置</summary><p>{report.tool.name} · Ø{report.tool.diameter_mm} mm · 刃长 {report.tool.cutting_length_mm} mm</p>
      <p>G54 原点 [{report.work_origin_mm.join(', ')}] mm；程序 Z 深度 [{report.depths_mm.join(', ')}] mm。</p>
      <p>{report.feed_mm_min} mm/min · {report.spindle_rpm} rpm · 每层 {report.stepdown_mm} mm · 保留余量 {report.radial_allowance_mm} mm。</p></details>
    {canExport ? <div className="flex flex-wrap gap-2"><button type="button" className="workspace-button" onClick={()=>download('cam_program')}>下载 GRBL 程序</button>
      <button type="button" className="workspace-button" onClick={()=>download('engineering_bundle')}>下载加工证据</button></div> : null}
    {error ? <p role="alert" className="mt-2 type-caption text-red-700">{error}</p> : null}
  </div>;
  if (report.kind!=='linear_static' || field.schema_version!=='cad-fea-field.v1') return <p role="alert" className="type-caption">工程报告与场数据类型不一致</p>;
  return <div className="mt-3" data-testid="engineering-result" data-workflow={taskId} data-revision={result.source_revision_id}>
    <p className="type-caption" data-testid="engineering-source-version">{sourceLabel}</p>
    <p className="my-2 type-caption">{report.nodes.toLocaleString()} 节点 · {report.elements.toLocaleString()} 二次四面体 · {report.solver.name} {report.solver.version}</p>
    <EngineeringFieldViewer field={field} />
    <dl className="my-3 grid grid-cols-2 gap-1 type-caption"><dt>最大位移</dt><dd>{report.maximum.displacement_mm.toPrecision(6)} mm</dd>
      <dt>最大等效应力</dt><dd>{report.maximum.von_mises_mpa.toPrecision(6)} MPa</dd><dt>反力相对不平衡</dt><dd>{report.force_balance_relative_error.toExponential(2)}</dd></dl>
    <p className="type-caption">{report.scope}</p>
    <details className="my-2 type-caption"><summary className="cursor-pointer">计算输入与边界条件</summary>
      <p>{report.material.name} · E {report.material.young_modulus_mpa} MPa · ν {report.material.poisson_ratio}</p>
      <p>网格尺寸 {report.mesh_size_mm} mm；固定 {report.fixed_face.axis}/{report.fixed_face.side}，加载 {report.loaded_face.axis}/{report.loaded_face.side}。</p>
      <p>载荷 [{report.force_n.join(', ')}] N；反力 [{report.reaction_n.map(v=>v.toPrecision(5)).join(', ')}] N。</p>
    </details>
    {canExport ? <button className="workspace-button" type="button" onClick={()=>download('engineering_bundle')}>下载求解证据</button> : null}
    {error ? <p className="mt-2 type-caption text-red-700" role="alert">{error}</p> : null}
  </div>;
}

export default function EngineeringTasks({document, initiallyOpen = false, onTasksChange}: {
  document:CloudDocument; initiallyOpen?:boolean;
  onTasksChange?:(documentId:string,tasks:EngineeringTaskSummary[])=>void;
}) {
  const [expanded,setExpanded]=useState(initiallyOpen);
  const [mode,setMode]=useState<'linear_static' | 'contour_milling'>('linear_static');
  const components=document.features.filter(f=>['PartDesign::Body','Part::Feature','PartDesign::Feature','App::Link'].includes(f.type || '') && (f.shape?.volume || 0)>0);
  const [component,setComponent]=useState(components[0]?.kernel_name || '');
  const [material,setMaterial]=useState(''),[elastic,setElastic]=useState(''),[poisson,setPoisson]=useState(''),[mesh,setMesh]=useState('');
  const [force,setForce]=useState(['0','0','']);
  const [fixed,setFixed]=useState<BoundaryPlane>({axis:'z',side:'min'}),[loaded,setLoaded]=useState<BoundaryPlane>({axis:'z',side:'max'});
  const [tasks,setTasks]=useState<EngineeringTaskSummary[]>([]),[selected,setSelected]=useState(''),[pending,setPending]=useState(false),[error,setError]=useState('');
  const [reload,setReload]=useState(0),[requestKey,setRequestKey]=useState(()=>crypto.randomUUID());
  const [base,setBase]=useState(document);
  const stale=base.head_revision_id!==document.head_revision_id || base.state_version!==document.state_version;
  useEffect(()=>{
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      try {
        const rows=await listEngineeringTasks(document.document_id,controller.signal);
        if (controller.signal.aborted) return;
        setTasks(rows);
        onTasksChange?.(document.document_id,rows);
        if (rows.some(t=>active(t.status))) timer=setTimeout(()=>void poll(),2000);
      } catch(e) {if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '工程任务加载失败');}
    };
    void poll();return ()=>{controller.abort();clearTimeout(timer);};
  },[document.document_id,document.head_revision_id,reload,onTasksChange]);
  const valid=material.trim() && [elastic,poisson,mesh,...force].every(s=>s.trim() && Number.isFinite(Number(s))) && Number(elastic)>0 && Number(mesh)>0
    && Number(poisson)>=-0.99 && Number(poisson)<=0.499 && force.some(s=>Number(s)!==0) && (fixed.axis!==loaded.axis || fixed.side!==loaded.side)
    && components.some(f=>f.kernel_name===component);
  const submit=async()=>{
    setPending(true);setError('');
    try {
      const task:LinearStaticTask={kind:'linear_static',component_name:component,mesh_size_mm:Number(mesh),fixed_face:fixed,loaded_face:loaded,
        force_n:force.map(Number) as [number,number,number],material:{name:material.trim(),young_modulus_mpa:Number(elastic),poisson_ratio:Number(poisson)}};
      const response=await submitEngineeringTask(base,task,requestKey);
      setSelected(response.workflow_run_id);setReload(v=>v+1);
    } catch(e) {setError(e instanceof Error ? e.message : '工程计算提交失败');}
    finally {setPending(false);}
  };
  return <section className="ww-inspector-section" aria-label="工程计算" data-testid="engineering-tasks">
    <details open={expanded} onToggle={e=>setExpanded(e.currentTarget.open)}><summary className="cursor-pointer type-section-heading">有限元与外轮廓加工</summary>
      <label className="my-2 block type-caption">工程计算类型<select aria-label="工程计算类型" className={inputClass} value={mode} onChange={e=>setMode(e.target.value as typeof mode)}>
        <option value="linear_static">有限元静力分析</option><option value="contour_milling">2.5D 外轮廓铣削</option></select></label>
      {mode==='linear_static' ? <p className="my-2 type-caption">对已提交实体执行静力分析。请提供材料与边界条件；轴向极值处须有实际平面。</p> : null}
      {document.can_edit && mode==='contour_milling' ? <ContourMillingForm document={document} onSubmitted={id=>{setSelected(id);setReload(v=>v+1);}}/> : null}
      {document.can_edit && mode==='linear_static' ? <form className="space-y-2" onSubmit={e=>{e.preventDefault();void submit();}} onChange={()=>setRequestKey(crypto.randomUUID())}>
        <label className="block type-caption">计算部件<select aria-label="计算部件" className={inputClass} value={component} onChange={e=>setComponent(e.target.value)}>
          {!component ? <option value="">选择实体</option> : null}{components.map(f=><option key={f.id} value={f.kernel_name}>{f.label} · {f.kernel_name}</option>)}</select></label>
        <label className="block type-caption">材料名称<input aria-label="有限元材料名称" className={inputClass} value={material} maxLength={120} onChange={e=>setMaterial(e.target.value)} /></label>
        {([['弹性模量 / MPa',elastic,setElastic],['泊松比',poisson,setPoisson],['网格尺寸 / mm',mesh,setMesh]] as const).map(([label,value,set])=><label className="block type-caption" key={label}>{label}
          <input aria-label={label} className={inputClass} type="number" step="any" value={value} onChange={e=>set(e.target.value)} /></label>)}
        <div className="grid grid-cols-2 gap-2"><Plane label="固定面" value={fixed} onChange={setFixed}/><Plane label="加载面" value={loaded} onChange={setLoaded}/></div>
        <div className="grid grid-cols-3 gap-2">{['X','Y','Z'].map((axis,index)=><label className="block type-caption" key={axis}>载荷 {axis} / N<input aria-label={`载荷 ${axis} / N`} className={inputClass} type="number" step="any" value={force[index]}
          onChange={e=>setForce(v=>v.map((n,i)=>i===index ? e.target.value : n))}/></label>)}</div>
        {stale ? <p className="type-caption">模型已更新。<button type="button" className="underline" onClick={()=>{setBase(document);setRequestKey(crypto.randomUUID());}}>使用当前修订重新确认输入</button></p> : null}
        <button className="workspace-button" type="submit" disabled={!valid || stale || pending}>{pending ? '正在提交计算' : '运行有限元计算'}</button>
      </form> : !document.can_edit ? <p className="type-caption">可查看已完成的工程结果；启动计算需要编辑权限。</p> : null}
      {error ? <p role="alert" className="mt-2 type-caption text-red-700">{error}</p> : null}
      <div className="mt-3 space-y-2" aria-label="工程任务列表">
        {!tasks.length ? <p className="type-caption">暂无工程任务</p> : tasks.map(t=><div key={t.workflow_run_id} className="rounded border border-[var(--line)] p-2 type-caption" data-engineering-task={t.workflow_run_id}>
          <p>v{t.source_state_version} · {t.source_revision_id.slice(0,8)} · {t.task_kind==='contour_milling' ? '外轮廓' : '有限元'} · {t.task.component_name} · {statuses[t.status] || t.status}</p>
          <p className="mt-1">{engineeringSourceLabel(t.source_revision_id,document.revision_id,document.head_revision_id)}</p>
          {t.error_message ? <p role="alert" className="mt-1 break-words text-red-700">{t.error_message.replace(/^[a-z_]+:\s*/,'')}</p> : null}
          {t.status==='succeeded' ? <button type="button" className="underline" onClick={()=>setSelected(t.workflow_run_id)}>查看计算结果</button> : null}
          {active(t.status) && document.can_edit ? <button type="button" className="underline" disabled={t.status==='cancelling'} onClick={()=>void cancelEngineeringTask(t.workflow_run_id).then(()=>setReload(v=>v+1)).catch((e:unknown)=>setError(e instanceof Error ? e.message : '取消失败'))}>取消计算</button> : null}
        </div>)}
      </div>
      {tasks.some(t=>t.workflow_run_id===selected && t.status==='succeeded') ? <Result key={`${document.document_id}:${selected}`} documentId={document.document_id} taskId={selected} viewedRevision={document.revision_id} headRevision={document.head_revision_id} canExport={document.can_export ?? document.can_edit}/> : null}
    </details>
  </section>;
}
