import { useState } from "react";
import { useFeatureLease } from "../../hooks/useFeatureLease";
import { updateDocumentInstance } from "../../services/engineeringService";
import type { CloudDocument, SemanticFeature } from "../../types/document";

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
  const lease = useFeatureLease(document, feature.id);
  const valid = [...position, ...axis, angle].every((v) => v.trim() && Number.isFinite(Number(v)))
    && axis.some((v) => Number(v) !== 0) && (editing ? !!current : /^[A-Za-z_][A-Za-z0-9_]{0,79}$/.test(name));
  const submit = async () => {
    setPending(true); setError("");
    try {
      const token = await lease.ensure();
      const result = await updateDocumentInstance(document, editing ? "assembly.place" : "assembly.instance", {
        object: editing ? feature.kernel_name : name, ...(!editing ? { source: feature.kernel_name } : {}),
        translation_mm: position.map(Number), rotation_axis: axis.map(Number), rotation_deg: Number(angle),
      }, key, token);
      onSubmitted(result.workflow_run_id, document); setKey(crypto.randomUUID());
    } catch (e) { setError(e instanceof Error ? e.message : "实例修改提交失败"); }
    finally { await lease.release().catch(() => {}); setPending(false); }
  };
  return <div className="ww-inspector-section">
    <h3>{editing ? "实例位置" : "创建装配实例"}</h3>
    {editing && !current ? <p className="type-caption">此历史检查点未记录实例位置，请在新版内核中提交一次修改后读取。</p> : null}
    <p className="type-caption text-[var(--muted)]">实例共享来源部件的几何。位置修改经云端检查并提交后生效。</p>
    {!editing ? <label className="my-2 block type-caption">实例名称<input aria-label="实例名称" className="mt-1 w-full rounded border border-[var(--line)] p-2"
      value={name} disabled={pending || !document.can_edit} onChange={(e) => { setName(e.target.value); setKey(crypto.randomUUID()); }} /></label> : null}
    {([position, axis] as const).map((values, group) => <div className="my-2 grid grid-cols-3 gap-2" key={group}>
      {values.map((value, index) => <label className="type-caption" key={index}>{group ? "旋转轴 " : "位置 "}{["X", "Y", "Z"][index]}{group ? "" : " mm"}
        <input aria-label={`${group ? "实例旋转轴" : "实例位置"}${["X", "Y", "Z"][index]}`} type="number" step="any" value={value}
          disabled={pending || !document.can_edit} className="mt-1 w-full rounded border border-[var(--line)] p-2"
          onChange={(e) => { (group ? setAxis : setPosition)((v) => v.map((x, i) => i === index ? e.target.value : x)); setKey(crypto.randomUUID()); }} />
      </label>)}
    </div>)}
    <label className="my-2 block type-caption">转角（度）<input aria-label="实例转角" type="number" min={-360} max={360} step="any" value={angle}
      disabled={pending || !document.can_edit} className="mt-1 w-full rounded border border-[var(--line)] p-2"
      onChange={(e) => { setAngle(e.target.value); setKey(crypto.randomUUID()); }} /></label>
    <button type="button" className="workspace-button" disabled={!valid || pending || !document.can_edit} onClick={() => void submit()}>{pending ? "正在提交" : editing ? "提交实例位置" : "提交实例创建"}</button>
    {error || lease.error ? <p role="alert" className="mt-2 type-caption text-red-700">{error || lease.error}</p> : null}
  </div>;
}
