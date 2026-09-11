import { useEffect, useState } from "react";
import type { CloudDocument } from "../../types/document";
import type { BranchComparison, DocumentBranches as Branches, SemanticChange } from "../../types/branches";
import { getDocumentBranches, createDocumentBranch, compareDocumentBranches, mergeDocumentBranch } from "../../services/engineeringService";

const FIELDS: Record<string,string> = {type:"类型",dependencies:"依赖",parameters:"参数",geometry_sha256:"几何",
  global_placement:"位置",sketch_constraints_sha256:"草图约束",instance:"实例",role:"用途",intent:"设计意图"};

function Changes({changes}: {changes: SemanticChange[]}) {
  return <ul className="mt-2 space-y-2 type-caption" data-i18n-skip>{changes.map(change => <li key={change.feature_id}>
    <strong>{change.label}</strong> · {change.kind==="added" ? "来源新增" : change.kind==="removed" ? "来源缺少" : "存在差异"}
    {change.fields.map(field => <details key={field.name}><summary className="cursor-pointer">{FIELDS[field.name] || field.name}</summary>
      <div className="max-h-48 overflow-auto rounded bg-[var(--surface)] p-2"><p>目标：{JSON.stringify(field.before)}</p><p>来源：{JSON.stringify(field.after)}</p></div>
    </details>)}
  </li>)}</ul>;
}

export default function DocumentBranches({document, onSubmitted}: {
  document: CloudDocument; onSubmitted?: (taskId: string, document: CloudDocument) => void;
}) {
  const [branches,setBranches] = useState<Branches | null>(null);
  const [name,setName] = useState("");
  const [key,setKey] = useState(() => crypto.randomUUID());
  const [source,setSource] = useState("");
  const [comparison,setComparison] = useState<BranchComparison | null>(null);
  const [mode,setMode] = useState<"parameters" | "source_geometry">("parameters");
  const [pending,setPending] = useState(false);
  const [error,setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void getDocumentBranches(document.document_id,controller.signal).then(setBranches).catch((e: unknown) => {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "分支读取失败");
    });
    return () => controller.abort();
  },[document.document_id,document.head_revision_id]);
  const create = async () => {
    setPending(true);setError("");
    try {
      const branch = await createDocumentBranch(document,name.trim(),key);
      window.location.assign(`/?${new URLSearchParams({document:branch.document_id,workspace:branch.tenant_id,task:branch.workflow_run_id})}`);
    } catch (e) {setError(e instanceof Error ? e.message : "分支创建失败");}
    finally {setPending(false);}
  };
  const compare = async () => {
    setPending(true);setError("");setComparison(null);
    try {setComparison(await compareDocumentBranches(document.document_id,source));}
    catch(e) {setError(e instanceof Error ? e.message : "比较失败");}
    finally {setPending(false);}
  };
  const merge = async () => {
    if (!comparison || !onSubmitted) return;
    setPending(true);setError("");
    try {const task = await mergeDocumentBranch(comparison,mode);onSubmitted(task.workflow_run_id,document);}
    catch(e) {setError(e instanceof Error ? e.message : "合并失败");}
    finally {setPending(false);}
  };
  const stale = comparison && (comparison.target_revision_id !== document.head_revision_id || comparison.target_state_version !== document.state_version);
  return <section className="ww-inspector-section" aria-label="文档分支">
    <h3>分支与合并</h3>
    <ul className="my-2 space-y-1 type-caption">{branches?.branches.map(branch => <li key={branch.document_id}>
      <a aria-current={branch.document_id===document.document_id ? "page" : undefined} className="underline" data-i18n-skip
        href={`/?${new URLSearchParams({document:branch.document_id,workspace:branches.tenant_id,
          ...(branch.state_version===0 && branch.workflow_run_id ? {task:branch.workflow_run_id} : {})})}`}>
        {branch.name}</a> · v{branch.state_version}{branch.state_version===0 ? " · 待完成初始模型任务" : ""}
    </li>)}</ul>
    {document.can_edit && onSubmitted ? <><label className="block type-caption">新分支名称<input aria-label="新分支名称" className="my-2 w-full rounded border border-[var(--line)] p-2"
      maxLength={120} value={name} onChange={e => {setName(e.target.value);setKey(crypto.randomUUID());}} /></label>
      <button className="workspace-button" type="button" disabled={pending || !name.trim() || !document.fcstd} onClick={() => void create()}>创建并校验分支</button>
    </> : null}
    <label className="mt-3 block type-caption">比较来源分支<select aria-label="比较来源分支" className="my-2 w-full rounded border border-[var(--line)] p-2" value={source}
      onChange={e => {setSource(e.target.value);setComparison(null);}}><option value="">选择来源</option>
      {branches?.branches.filter(b => b.document_id!==document.document_id && b.state_version>0).map(b => <option key={b.document_id} value={b.document_id}>{b.name}</option>)}
    </select></label>
    <button className="workspace-button" type="button" disabled={!source || pending} onClick={() => void compare()}>比较分支差异</button>
    {comparison ? <div className="mt-3" data-testid="branch-comparison"><p className="type-caption">共同修订 {comparison.common_revision_id.slice(0,8)} · {comparison.differences.length} 个特征存在差异</p>
      <Changes changes={comparison.differences} />
      <p className="my-2 type-caption">{comparison.annotation_policy}</p>
      {comparison.conflicts.map(c => <p role="status" key={c.code} className="my-2 type-caption text-amber-800">{c.message}</p>)}
      {comparison.parameters.reason ? <p className="type-caption">{comparison.parameters.reason}</p> : null}
      {document.can_edit && onSubmitted ? <><select aria-label="合并方式" className="my-2 w-full rounded border border-[var(--line)] p-2 type-caption" value={mode} onChange={e => setMode(e.target.value as typeof mode)}>
        <option value="parameters">合并独立参数变更</option><option value="source_geometry">采用来源分支完整几何</option></select>
        <p className="mb-2 type-caption">{mode==="source_geometry" ? comparison.source_geometry_policy : "在目标当前模型上重新执行来源参数变更，校验后送审。"}</p>
        {stale ? <p role="status" className="type-caption">目标版本已更新，请重新比较。</p> : null}
        <button className="workspace-button" type="button" disabled={pending || !!stale || (mode==="parameters" && !comparison.can_merge_parameters)} onClick={() => void merge()}>提交合并校验</button>
      </> : null}
    </div> : null}
    {pending ? <p role="status" className="mt-2 type-caption">正在处理分支请求…</p> : null}
    {error ? <p role="alert" className="mt-2 type-caption text-red-700">{error}</p> : null}
  </section>;
}
