import { useEffect, useState } from "react";
import type { CloudDocument, SemanticFeature, SelectionContext } from "../../types/document";
import { listEngineeringTasks, submitNativeMeasurement, readNativeMeasurement, type NativeMeasurementRequest } from "../../services/clients/engineering";

export default function FeatureMeasurements({ document, feature, onSelectTopology }: { document: CloudDocument; feature: SemanticFeature; onSelectTopology?: (id: string, selector: NonNullable<SelectionContext['topology_selector']>) => void }) {
  const bindings = document.features.flatMap(item => (item.topology_bindings || []).map(selector => ({ feature: item, selector })));
  const [mode, setMode] = useState<NativeMeasurementRequest['measurement']>('volume'),[other,setOther]=useState('');
  const [first, setFirst] = useState(''), [second, setSecond] = useState('');
  const [task, setTask] = useState<{ id: string; revision: string } | null>(null), [key, setKey] = useState(() => crypto.randomUUID());
  const [pending, setPending] = useState(false), [error, setError] = useState(''), [value, setValue] = useState<{ value: number; unit: string; method: string; revision: string } | null>(null);
  const choices = bindings.map((binding, index) => ({ ...binding, key: String(index) })).filter(binding => binding.selector.subelement_kind === (mode === 'circle_diameter' ? 'edge' : 'face') && (mode !== 'circle_diameter' || binding.feature.id === feature.id));
  const chosen = (id: string) => choices.find(choice => choice.key === id);
  const valid = ['volume','solid_count'].includes(mode) || ['component_clearance','intersection_volume'].includes(mode) && !!other || Boolean(chosen(first)?.feature.id === feature.id && (mode !== 'face_distance' || chosen(second) && first !== second));
  useEffect(() => {
    if (!task || task.revision !== document.revision_id) return;
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { try {
      const jobs = await listEngineeringTasks(document.document_id, abort.signal), job = jobs.find(item => item.workflow_run_id === task.id);
      if (!job) throw new Error('测量任务记录缺失');
      if (job.status === 'succeeded') {
        const result = await readNativeMeasurement(document.document_id, task.id, abort.signal);
        if (result.source_revision_id !== task.revision || !Number.isFinite(result.report.value) || result.report.status !== 'measured') throw new Error('测量结果来源或数值无效');
        if (!abort.signal.aborted) { setValue({ ...result.report, revision: task.revision }); setPending(false); }
      } else if (['failed','cancelled','timed_out'].includes(job.status)) throw new Error(job.error_message || '原生测量失败');
      else if (!abort.signal.aborted) timer = setTimeout(() => void poll(), 1000);
    } catch (reason) { if (!abort.signal.aborted) { setError(reason instanceof Error ? reason.message : '测量读取失败'); setPending(false); } } };
    void poll(); return () => { abort.abort(); clearTimeout(timer); };
  }, [document.document_id, document.revision_id, task]);
  const measure = async () => {
    if (!valid || pending) return; setPending(true); setError(''); setValue(null);
    try { const response = await submitNativeMeasurement(document, { kind: 'native_measure', component_name: feature.kernel_name, measurement: mode,
      selectors: mode === 'face_distance' ? [chosen(first)!.selector, chosen(second)!.selector] : mode === 'circle_diameter' ? [chosen(first)!.selector] : [],
      ...(['component_clearance','intersection_volume'].includes(mode)?{other_component_name:other}:{}) }, key);
      setTask({ id: response.workflow_run_id, revision: document.revision_id });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '测量提交失败'); setPending(false); }
  };
  return <section className="ww-inspector-section"><h3>原生测量与子元素选择</h3>
    <label className="block my-2">测量方式<select className="ww-field" aria-label="原生测量方式" disabled={pending} value={mode} onChange={event => { setMode(event.target.value as typeof mode); setFirst(''); setSecond(''); setKey(crypto.randomUUID()); }}>
      <option value="volume">对象体积</option><option value="solid_count">实体数量</option><option value="face_distance">两面的最短距离</option><option value="circle_diameter">圆边直径</option>
      <option value="component_clearance">部件最小间隙</option><option value="intersection_volume">部件相交体积</option>
    </select></label>
    {['component_clearance','intersection_volume'].includes(mode)?<label className="block my-2">第二个部件<select className="ww-field" aria-label="测量第二个部件" value={other} disabled={pending} onChange={e=>{setOther(e.target.value);setKey(crypto.randomUUID());}}><option value="">请选择最终实体或实例</option>{document.features.filter(f=>f.id!==feature.id && (f.type==='PartDesign::Body' || f.type==='App::Link' || !f.structure?.container_ids.length && (f.shape?.volume || 0)>0)).map(f=><option key={f.id} value={f.kernel_name}>{f.label}</option>)}</select></label>:null}
    {['face_distance','circle_diameter'].includes(mode) ? [0, ...(mode === 'face_distance' ? [1] : [])].map(index => <label className="block my-2" key={index}>选择{index === 0 ? '第一个' : '第二个'}子元素<select className="ww-field" aria-label={`测量子元素 ${index + 1}`} value={index === 0 ? first : second} disabled={pending} onChange={event => { (index === 0 ? setFirst : setSecond)(event.target.value); setKey(crypto.randomUUID()); }}><option value="">请选择已验证拓扑</option>{choices.filter(choice => index !== 0 || choice.feature.id === feature.id).map(choice => <option key={choice.key} value={choice.key}>{choice.feature.label} · {choice.selector.axis}/{choice.selector.extreme}{choice.selector.radius_mm ? ` · R ${choice.selector.radius_mm} mm` : ''}</option>)}</select></label>) : null}
    {chosen(first)?.feature.id === feature.id && onSelectTopology ? <button type="button" className="workspace-button my-2" onClick={() => onSelectTopology(feature.id, chosen(first)!.selector)}>用此子元素限定 Agent 修改</button> : null}
    <button type="button" className="workspace-button" disabled={!valid || ['component_clearance','intersection_volume'].includes(mode) && !other || pending || !document.can_edit || document.view_mode && document.view_mode !== 'committed'} onClick={() => void measure()}>{pending ? '原生测量中…' : '运行原生测量'}</button>
    <p className="type-caption mt-2">测量来自同一修订的 FreeCAD / OpenCASCADE 实体。圆边直径不能直接当作孔的验收；未支持或有歧义的子元素不开放修改。</p>
    {value && value.revision === document.revision_id ? <p role="status">{value.value.toPrecision(8)} {value.unit} · 修订 <span data-i18n-skip>{value.revision.slice(0,8)}</span> · {value.method}</p> : null}
    {error ? <p role="alert" className="text-red-700">{error}<button className="workspace-button" type="button" onClick={() => void measure()}>重试同一测量</button></p> : null}
  </section>;
}
