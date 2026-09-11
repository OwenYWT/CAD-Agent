import { useEffect, useState } from 'react';
import type { CloudDocument } from '../../types/document';
import type { EngineeringTaskSummary } from '../../types/engineeringTask';
import type { DocumentRelease, ReleaseBOM, ReleaseResult } from '../../types/release';
import { cancelEngineeringTask, downloadReleaseArtifact, listDocumentReleases, listEngineeringTasks, readDocumentRelease, readReleaseBOM, submitDocumentRelease } from '../../services/engineeringService';

const active=(status:string)=>['pending','planning','running','cancelling'].includes(status);
const statuses:Record<string,string>={pending:'排队中',planning:'准备发布',running:'正在打包',succeeded:'已发布',failed:'发布失败',cancelled:'已取消',cancelling:'正在取消',timed_out:'发布超时'};

function ReleaseDetails({document,releaseId}:{document:CloudDocument;releaseId:string}) {
  const [loaded,setLoaded]=useState<{result:ReleaseResult;bom:ReleaseBOM} | null>(null),[error,setError]=useState('');
  useEffect(()=>{
    const controller=new AbortController();
    void readDocumentRelease(document.document_id,releaseId,controller.signal).then(async result=>{
      const bom=await readReleaseBOM(result.artifacts.release_bom_json,controller.signal);
      if (bom.source.revision_id!==result.source_revision_id || bom.source.fcstd_sha256!==result.report.source_fcstd_sha256) throw new Error('BOM 与发布来源不一致');
      if (!controller.signal.aborted) setLoaded({result,bom});
    }).catch((e:unknown)=>{if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '发布读取失败');});
    return ()=>controller.abort();
  },[document.document_id,releaseId]);
  if (!loaded) return <p className="my-2 type-caption" role={error ? 'alert' : 'status'}>{error || '正在校验发布文件…'}</p>;
  const {result,bom}=loaded;
  return <div className="mt-3" data-testid="release-result" data-release={releaseId}>
    <p className="type-section-heading">{result.report.release_name}</p>
    <p className="my-2 type-caption">版本 {result.source_state_version} · {result.source_revision_id===document.head_revision_id ? '当前修订' : '历史修订'} · {bom.instance_count} 个实例 / {bom.definition_count} 个定义</p>
    <p className="type-caption">原生 BOM 包含全部最终实体和实例（含隐藏部件）。</p>
    <div className="my-2 overflow-x-auto"><table className="w-full text-left type-caption" aria-label="发布 BOM"><thead><tr><th>部件</th><th>数量</th><th>定义</th></tr></thead>
      <tbody>{bom.rows.map(row=><tr key={row.kernel_name}><td className="py-1">{row.name}<span className="block text-[var(--text-muted)]">{row.kernel_name}</span></td><td>{row.quantity}</td><td>{row.definition_name}</td></tr>)}</tbody></table></div>
    <p className="type-caption">附带 {new Set(result.report.engineering_artifacts.map(a=>a.workflow_run_id)).size} 项同修订工程任务证据；{result.report.files.length} 个文件均有内容哈希。</p>
    {document.can_edit ? <div className="my-3 flex flex-wrap gap-2">{([
      ['engineering_bundle','下载完整发布包'],['release_step','下载发布 STEP'],['release_stl','下载发布 STL'],['release_bom_csv','下载 BOM CSV'],['release_manifest','下载发布清单'],
    ] as const).map(([kind,label])=><button type="button" className="workspace-button" key={kind}
      onClick={()=>void downloadReleaseArtifact(result.artifacts[kind]).catch((e:unknown)=>setError(e instanceof Error ? e.message : '文件下载失败'))}>{label}</button>)}</div> : null}
    {error ? <p className="type-caption text-red-700" role="alert">{error}</p> : null}
  </div>;
}

export default function DocumentReleases({document}:{document:CloudDocument}) {
  const [releases,setReleases]=useState<DocumentRelease[]>([]),[tasks,setTasks]=useState<EngineeringTaskSummary[]>([]);
  const [name,setName]=useState(''),[evidence,setEvidence]=useState<string[]>([]),[selected,setSelected]=useState('');
  const [reload,setReload]=useState(0),[pending,setPending]=useState(false),[error,setError]=useState('');
  const [base,setBase]=useState(document),[key,setKey]=useState(()=>crypto.randomUUID());
  const stale=base.head_revision_id!==document.head_revision_id || base.state_version!==document.state_version;
  useEffect(()=>{
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      try {
        const [rows,engineering]=await Promise.all([listDocumentReleases(document.document_id,controller.signal),listEngineeringTasks(document.document_id,controller.signal)]);
        if (controller.signal.aborted) return;
        setReleases(rows);setTasks(engineering);
        if (rows.some(r=>active(r.status))) timer=setTimeout(()=>void poll(),2000);
      } catch(e) {if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '发布列表读取失败');}
    };void poll();return ()=>{controller.abort();clearTimeout(timer);};
  },[document.document_id,document.head_revision_id,reload]);
  const choices=tasks.filter(t=>t.status==='succeeded' && t.source_revision_id===base.head_revision_id);
  const submit=async()=>{
    setPending(true);setError('');
    try {const response=await submitDocumentRelease(base,name.trim(),evidence,key);setSelected(response.release_id);setReload(v=>v+1);}
    catch(e) {setError(e instanceof Error ? e.message : '发布提交失败');}
    finally {setPending(false);}
  };
  return <section className="ww-inspector-section" aria-label="工程发布" data-testid="document-releases"><details><summary className="cursor-pointer type-section-heading">版本发布与 BOM</summary>
    <p className="my-2 type-caption">固定当前模型、BOM、STEP/STL 与选定工程证据，生成可追溯的发布包。</p>
    {document.can_commit ? <form className="space-y-2" onSubmit={e=>{e.preventDefault();void submit();}} onChange={()=>setKey(crypto.randomUUID())}>
      <label className="block type-caption">发布名称<input aria-label="发布名称" value={name} maxLength={120} onChange={e=>setName(e.target.value)} className="mt-1 w-full rounded border border-[var(--line)] bg-white p-2"/></label>
      <fieldset className="space-y-1 type-caption"><legend>附带当前修订的工程证据（最多 8 项）</legend>
        {!choices.length ? <p>当前修订暂无已完成工程任务，可发布 CAD 与 BOM。</p> : choices.map(t=><label key={t.workflow_run_id} className="flex items-start gap-2">
          <input type="checkbox" data-evidence={t.workflow_run_id} checked={evidence.includes(t.workflow_run_id)} disabled={!evidence.includes(t.workflow_run_id) && evidence.length>=8}
            onChange={e=>setEvidence(ids=>e.target.checked ? [...ids,t.workflow_run_id] : ids.filter(id=>id!==t.workflow_run_id))}/>
          <span>{t.task_kind==='linear_static' ? '有限元' : '外轮廓'} · {t.task.component_name} · {new Date(t.created_at).toLocaleString()}</span></label>)}</fieldset>
      <button type="button" className="type-caption underline" onClick={()=>setReload(v=>v+1)}>刷新工程证据</button>
      {stale ? <p className="type-caption">模型已更新。<button type="button" className="underline" onClick={()=>{setBase(document);setEvidence([]);setKey(crypto.randomUUID());}}>核对并使用当前修订</button></p> : null}
      <div><button className="workspace-button" type="submit" disabled={pending || stale || !name.trim() || !document.fcstd}>{pending ? '正在提交发布' : '创建不可变发布'}</button></div>
    </form> : <p className="type-caption">发布需要项目所有者或管理员权限。</p>}
    {error ? <p role="alert" className="my-2 type-caption text-red-700">{error}</p> : null}
    <div className="mt-3 space-y-2" aria-label="发布列表">{!releases.length ? <p className="type-caption">暂无发布</p> : releases.map(r=><div className="rounded border border-[var(--line)] p-2 type-caption" key={r.release_id} data-release-row={r.release_id}>
      <p>{r.release_name} · 版本 {r.source_state_version} · {statuses[r.status] || r.status}</p>
      {r.error_message ? <p role="alert" className="break-words text-red-700">{r.error_message.replace(/^[a-z_]+:\s*/,'')}</p> : null}
      {r.status==='succeeded' ? <button type="button" className="underline" onClick={()=>setSelected(r.release_id)}>查看发布与 BOM</button> : null}
      {active(r.status) && document.can_commit ? <button type="button" className="underline" disabled={r.status==='cancelling'} onClick={()=>void cancelEngineeringTask(r.release_id).then(()=>setReload(v=>v+1)).catch((e:unknown)=>setError(e instanceof Error ? e.message : '取消发布失败'))}>取消发布</button> : null}
    </div>)}</div>
    {releases.some(r=>r.release_id===selected && r.status==='succeeded') ? <ReleaseDetails key={selected} document={document} releaseId={selected}/> : null}
  </details></section>;
}
