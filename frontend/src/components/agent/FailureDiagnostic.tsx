import { useState } from "react";
import { authFetch } from "../../auth";
import { API_BASE, readJson } from "../../services/clients/http";
import { previewSketch, sketchBounds } from "../../adapters/sketchPreview";
import type { SketchGeometry, SketchConstraint } from "../../types/sketch";
import { WorkspaceDialog } from "../common/WorkspaceOverlay";

interface Diagnostic {
  sha256: string; base_revision_id: string; task_status: string;
  requirement_bindings?:Array<{sketch:string;logical_constraint_id:string;dimension_role:string;geometry_index:number;check_id:string;source_kind:string;binding_status:string}>;protection_note?:string;
  snapshot: { failed_operation_id: string; valid_checkpoint: false; failure: { code: string; message: string };
    operations: { operation_id: string; status: string }[];
    sketches: { name: string; geometry: SketchGeometry[]; constraints: (SketchConstraint & { logical_id: string; origin: string; operation_id: string | null })[]; solver: Record<string, unknown> }[] };
}
export default function FailureDiagnostic({ taskId, canModify, onRecover }: { taskId: string; canModify: boolean; onRecover?: (featureId?:string) => void }) {
  const [open, setOpen] = useState(false), [data, setData] = useState<Diagnostic | null>(null);
  const [pending, setPending] = useState(false), [error, setError] = useState("");
  const [selection,setSelection]=useState<{sketch:string;geometry:number[]}|null>(null);
  const load = async () => {
    setOpen(true); setPending(true); setError("");
    try { const result = await readJson<Diagnostic>(await authFetch(`${API_BASE}/api/tasks/${taskId}/diagnostic`), "失败现场读取失败");
      if (result.snapshot.valid_checkpoint !== false) throw new Error("诊断不能作为有效模型"); setData(result);setSelection(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "失败现场读取失败"); } finally { setPending(false); }
  };
  const takeover = async () => {
    setPending(true); setError("");
    try {
      const baseline=await readJson<{sketches:Array<{name:string;feature_id:string|null}>}>(await authFetch(`${API_BASE}/api/tasks/${taskId}/takeover-baseline`), "暂时不能接管");
      const editable=baseline.sketches.find(s=>s.feature_id);
      if (!editable?.feature_id) {setError('失败草图不在有效基线中。请基于原需求重新规划几何；不能把失败现场保存为模型。');return;}
      setOpen(false);onRecover?.(editable.feature_id);
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "暂时不能接管"); } finally { setPending(false); }
  };
  return <><button type="button" className="workspace-button" onClick={() => void load()}>查看失败草图与求解诊断</button>
    <WorkspaceDialog open={open} onClose={() => setOpen(false)} title="失败现场诊断">
      {pending ? <p role="status">正在核对现场与任务归属…</p> : null}
      {error ? <p role="alert" className="text-red-700">{error}</p> : null}
      {data ? <div className="space-y-4">
        <p>失败操作：<code>{data.snapshot.failed_operation_id}</code></p><p>{data.snapshot.failure.message}</p>
        <p>此现场只供诊断。手动接管从有效保存基线开始；新建失败草图需要重新规划。</p>
        <p>{data.protection_note}</p>
        {data.snapshot.sketches.map(sketch => {
          const geometry = previewSketch(sketch.geometry, sketch.constraints, {}), bounds = sketchBounds(sketch.geometry);
          return <section key={sketch.name}><h3 data-i18n-skip>{sketch.name}</h3>
            <svg viewBox={bounds.join(" ")} className="h-[min(60vh,480px)] w-full rounded border border-[var(--line)]" aria-label="失败草图几何" role="group">
              {geometry.map(item => {
                const linked=selection?.sketch===sketch.name && selection.geometry.includes(item.index);
                const props={stroke:linked?'#7653bd':'#b42318',strokeWidth:bounds[2]*.006,role:'button',tabIndex:0,'aria-label':`定位失败几何 ${item.index}`,onClick:()=>setSelection({sketch:sketch.name,geometry:[item.index]}),onKeyDown:(event:React.KeyboardEvent)=>{if(event.key==='Enter' || event.key===' ') {event.preventDefault();setSelection({sketch:sketch.name,geometry:[item.index]});}}};
                return item.start && item.end ? <line key={item.index} {...props} x1={item.start[0]} y1={-item.start[1]} x2={item.end[0]} y2={-item.end[1]}/> : item.center && item.radius_mm ? <circle key={item.index} {...props} cx={item.center[0]} cy={-item.center[1]} r={item.radius_mm} fill="none"/> : null;
              })}
            </svg><p>自由度：{typeof sketch.solver.degrees_of_freedom==='number'?String(sketch.solver.degrees_of_freedom):'求解器未提供'} · 状态：{String(sketch.solver.constraint_status || '未知')}</p><p>求解器记录：</p><pre className="whitespace-pre-wrap break-words">{JSON.stringify(sketch.solver, null, 2)}</pre>
            <div className="overflow-x-auto"><table className="w-full type-caption"><thead><tr><th>逻辑约束</th><th>关系</th><th>驱动 / 参考</th><th>来源</th></tr></thead><tbody>{sketch.constraints.map(constraint => <tr key={constraint.logical_id} className="ww-sketch-constraint" data-linked={selection?.sketch===sketch.name && selection.geometry.some(id=>id===constraint.first || id===constraint.second)}><td className="break-all"><button className="workspace-button max-w-full break-all" type="button" onClick={()=>setSelection({sketch:sketch.name,geometry:[constraint.first,constraint.second].filter(id=>id>=0)})}><span data-i18n-skip>{constraint.logical_id}</span></button></td><td>{constraint.type} {constraint.value}</td><td>{constraint.driving === true ? "驱动" : constraint.driving === false ? "参考" : "未知，继续保护"}</td><td data-i18n-skip>{constraint.operation_id || constraint.origin}</td></tr>)}</tbody></table></div>
            {data.requirement_bindings?.filter(binding=>binding.sketch===sketch.name).map(binding=><p className="type-caption" key={`${binding.logical_constraint_id}:${binding.check_id}`}><span data-i18n-skip>{binding.logical_constraint_id} → {binding.check_id} · {binding.dimension_role}</span> · 几何 <span data-i18n-skip>{binding.geometry_index} · {binding.binding_status}</span>（保持保护）</p>)}
          </section>;
        })}
        <details><summary>已执行、失败中与未执行的操作</summary><ol>{data.snapshot.operations.map(operation => <li key={operation.operation_id}><code>{operation.operation_id}</code> · {({ executed: "已执行", failing: "失败中", not_executed: "未执行" } as Record<string, string>)[operation.status] || operation.status}</li>)}</ol></details>
        <details><summary>来源修订与证据哈希</summary><code>{data.base_revision_id}<br />{data.sha256}</code></details>
        {canModify && onRecover ? <button type="button" disabled={pending || !["failed", "cancelled", "timed_out"].includes(data.task_status)} className="workspace-button" onClick={() => void takeover()}>接管有效基线并修正参数</button> : null}
      </div> : null}
    </WorkspaceDialog></>;
}
