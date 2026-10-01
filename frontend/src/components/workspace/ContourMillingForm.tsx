import { useState } from 'react';
import type { CloudDocument } from '../../types/document';
import type { ContourMillingTask } from '../../types/engineeringTask';
import { submitEngineeringTask } from "../../services/clients/engineering";

const fields=[['diameter','刀具直径 / mm'],['cuttingLength','有效刃长 / mm'],['stepdown','每层切深 / mm'],
  ['feed','轮廓进给 / mm/min'],['plunge','垂直进给 / mm/min'],['spindle','主轴转速 / rpm'],['safe','安全高度 / mm'],
  ['stock','毛坯侧边余量 / mm'],['allowance','径向保留余量 / mm'],['tolerance','弦差精度 / mm']] as const;
const inputClass='mt-1 w-full rounded border border-[var(--line)] bg-white p-2';

export default function ContourMillingForm({document,onSubmitted}: {document:CloudDocument;onSubmitted:(id:string)=>void}) {
  const components=document.features.filter(f=>['PartDesign::Body','Part::Feature','PartDesign::Feature','App::Link'].includes(f.type || '') && (f.shape?.volume || 0)>0);
  const [component,setComponent]=useState(components[0]?.kernel_name || ''),[tool,setTool]=useState('');
  const [values,setValues]=useState<Record<string,string>>({safe:'5',allowance:'0',tolerance:'0.01'});
  const [origin,setOrigin]=useState(['0','0','0']);
  const [base,setBase]=useState(document),[key,setKey]=useState(()=>crypto.randomUUID()),[pending,setPending]=useState(false),[error,setError]=useState('');
  const stale=base.head_revision_id!==document.head_revision_id || base.state_version!==document.state_version;
  const valid=tool.trim() && components.some(f=>f.kernel_name===component) && fields.every(([name])=>values[name]?.trim() && Number.isFinite(Number(values[name]))
    && (name==='allowance' ? Number(values[name])>=0 : Number(values[name])>0)) && origin.every(v=>v.trim() && Number.isFinite(Number(v)))
    && Number.isInteger(Number(values.spindle));
  const submit=async()=>{
    setPending(true);setError('');
    try {
      const task:ContourMillingTask={kind:'contour_milling',component_name:component,postprocessor:'grbl_1_1',
        tool:{name:tool.trim(),diameter_mm:Number(values.diameter),cutting_length_mm:Number(values.cuttingLength)},
        work_origin_mm:origin.map(Number) as [number,number,number],stepdown_mm:Number(values.stepdown),feed_mm_min:Number(values.feed),
        plunge_mm_min:Number(values.plunge),spindle_rpm:Number(values.spindle),safe_height_mm:Number(values.safe),
        stock_margin_mm:Number(values.stock),radial_allowance_mm:Number(values.allowance),chord_tolerance_mm:Number(values.tolerance)};
      const result=await submitEngineeringTask(base,task,key);onSubmitted(result.workflow_run_id);
    } catch(e) {setError(e instanceof Error ? e.message : '加工路径计算失败');}
    finally {setPending(false);}
  };
  return <form className="space-y-2" aria-label="外轮廓加工设置" onChange={()=>setKey(crypto.randomUUID())} onSubmit={e=>{e.preventDefault();void submit();}}>
    <p className="type-caption">为 Z 向等截面实体生成外轮廓分层路径。输出采用 GRBL 1.1、毫米、G54 绝对坐标；平底立铣刀由实际机床安装。</p>
    <label className="block type-caption">加工部件<select aria-label="加工部件" className={inputClass} value={component} onChange={e=>setComponent(e.target.value)}>
      {!component ? <option value="">选择实体</option> : null}{components.map(f=><option key={f.id} value={f.kernel_name}>{f.label} · {f.kernel_name}</option>)}</select></label>
    <label className="block type-caption">刀具名称<input aria-label="刀具名称" className={inputClass} maxLength={120} value={tool} onChange={e=>setTool(e.target.value)}/></label>
    <div className="grid grid-cols-2 gap-2">{fields.map(([name,label])=><label className="block type-caption" key={name}>{label}
      <input aria-label={label} type="number" step={name==='spindle' ? '1' : 'any'} className={inputClass} value={values[name] || ''} onChange={e=>setValues(v=>({...v,[name]:e.target.value}))}/></label>)}</div>
    <p className="type-caption">G54 原点在模型坐标系中的位置 / mm</p>
    <div className="grid grid-cols-3 gap-2">{['X','Y','Z'].map((axis,i)=><label className="block type-caption" key={axis}>{axis}
      <input aria-label={`G54 原点 ${axis} / mm`} type="number" step="any" className={inputClass} value={origin[i]} onChange={e=>setOrigin(v=>v.map((x,j)=>i===j ? e.target.value : x))}/></label>)}</div>
    {stale ? <p className="type-caption">模型已更新。<button type="button" className="underline" onClick={()=>{setBase(document);setKey(crypto.randomUUID());}}>使用当前修订重新确认加工输入</button></p> : null}
    <button className="workspace-button" type="submit" disabled={!valid || stale || pending}>{pending ? '正在提交加工计算' : '生成外轮廓加工路径'}</button>
    {error ? <p role="alert" className="type-caption text-red-700">{error}</p> : null}
  </form>;
}
