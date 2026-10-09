import { useRef, useState } from "react";
import { useFeatureLease } from "../../hooks/useFeatureLease";
import { updateDocumentInstance } from "../../services/clients/documents";
import type { CloudDocument, SemanticFeature } from "../../types/document";
import { useDraftGuard } from '../../hooks/useDraftGuard';
import { EngineeringApiError } from '../../services/clients/http';

export default function InstanceProperties({ document, feature, onSubmitted }: {
  document: CloudDocument; feature: SemanticFeature; onSubmitted: (id: string, document: CloudDocument) => void;
}) {
  const editing = feature.type === "App::Link";
  const current = feature.instance;
  const [name, setName] = useState("");
  const [position, setPosition] = useState((current?.translation_mm || [0, 0, 0]).map(String));
  const [axis, setAxis] = useState((current?.rotation_axis || [0, 0, 1]).map(String));
  const [angle, setAngle] = useState(String(current?.rotation_deg || 0));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [base]=useState(document),[submitted,setSubmitted]=useState(false),[uncertain,setUncertain]=useState(false),[token,setToken]=useState<string|null>(null);
  const busy=useRef(false),stale=base.head_revision_id!==document.head_revision_id || base.state_version!==document.state_version;
  const locked=pending || submitted || uncertain || stale || !document.can_edit;
  const lease = useFeatureLease(document, feature.id);
  const valid = [...position, ...axis, angle].every((v) => v.trim() && Number.isFinite(Number(v)))
    && axis.some((v) => Number(v) !== 0) && (editing ? !!current : /^[A-Za-z_][A-Za-z0-9_]{0,79}$/.test(name));
  const dirty=Boolean(name || JSON.stringify(position)!==JSON.stringify((current?.translation_mm || [0,0,0]).map(String)) || JSON.stringify(axis)!==JSON.stringify((current?.rotation_axis || [0,0,1]).map(String)) || angle!==String(current?.rotation_deg || 0));
  useDraftGuard(dirty && !submitted || pending,`${feature.label} 实例位置`,()=>{setName('');setPosition((current?.translation_mm || [0,0,0]).map(String));setAxis((current?.rotation_axis || [0,0,1]).map(String));setAngle(String(current?.rotation_deg || 0));setKey(crypto.randomUUID());});
  const submit = async () => {
    if (!valid || busy.current || submitted || !document.can_edit || stale && !uncertain) return;
    busy.current=true;
    setPending(true); setError("");
    try {
      const leaseToken = token || await lease.ensure();setToken(leaseToken);
      const result = await updateDocumentInstance(base, editing ? "assembly.place" : "assembly.instance", {
        object: editing ? feature.kernel_name : name, ...(!editing ? { source: feature.kernel_name } : {}),
        translation_mm: position.map(Number), rotation_axis: axis.map(Number), rotation_deg: Number(angle),
      }, key, leaseToken);
      setSubmitted(true);setUncertain(false);onSubmitted(result.workflow_run_id, base);void lease.release().catch(()=>{});
    } catch (e) {const definitive=e instanceof EngineeringApiError && e.httpStatus!==undefined && e.httpStatus<500;setUncertain(!definitive);if(definitive)setToken(null);setError(e instanceof Error ? e.message : "实例修改提交失败"); }
    finally {busy.current=false;setPending(false);}
  };
  return <div className="ww-inspector-section">
    <h3>{editing ? "实例位置" : "创建装配实例"}</h3>
    {editing && !current ? <p className="type-caption">此历史检查点未记录实例位置，请在新版内核中提交一次修改后读取。</p> : null}
    <p className="type-caption text-[var(--muted)]">实例共享来源部件的几何。位置修改经云端检查并提交后生效。</p>
    {current ? <p className="type-caption">来源定义：<span data-i18n-skip>{current.source}</span> · 实例对象：<span data-i18n-skip>{feature.kernel_name}</span></p> : null}
    {!editing ? <label className="my-2 block type-caption">实例名称<input aria-label="实例名称" className="mt-1 w-full rounded border border-[var(--line)] p-2"
      value={name} disabled={locked} onChange={(e) => { setName(e.target.value); setKey(crypto.randomUUID()); }} /></label> : null}
    {([position, axis] as const).map((values, group) => <div className="my-2 grid grid-cols-3 gap-2" key={group}>
      {values.map((value, index) => <label className="type-caption" key={index}>{group ? "旋转轴 " : "位置 "}{["X", "Y", "Z"][index]}{group ? "" : " mm"}
        <input aria-label={`${group ? "实例旋转轴" : "实例位置"}${["X", "Y", "Z"][index]}`} type="number" step="any" value={value}
          disabled={locked} className="mt-1 w-full rounded border border-[var(--line)] p-2"
          onChange={(e) => { (group ? setAxis : setPosition)((v) => v.map((x, i) => i === index ? e.target.value : x)); setKey(crypto.randomUUID()); }} />
      </label>)}
    </div>)}
    <label className="my-2 block type-caption">转角（度）<input aria-label="实例转角" type="number" min={-360} max={360} step="any" value={angle}
      disabled={locked} className="mt-1 w-full rounded border border-[var(--line)] p-2"
      onChange={(e) => { setAngle(e.target.value); setKey(crypto.randomUUID()); }} /></label>
    <button type="button" className="workspace-button" disabled={!valid || pending || submitted || !document.can_edit || stale && !uncertain} onClick={() => void submit()}>{pending ? "正在提交" : submitted?'已受理，等待候选审核':uncertain?'核对并重试原实例请求':editing ? "提交实例位置" : "提交实例创建"}</button>
    {stale?<p role="status">保存基线已变化；此草稿不会自动换基线执行。</p>:null}
    {error || lease.error ? <p role="alert" className="mt-2 type-caption text-red-700">{error || lease.error}</p> : null}
  </div>;
}
