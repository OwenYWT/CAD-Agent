import { useEffect, useRef, useState } from "react";
import { parameterSubmissionState } from "../../adapters/parameterSubmission";
import { useSessionStore } from "../../stores/sessionStore";
import { useDraftGuard } from "../../hooks/useDraftGuard";
import { guardDraft } from "../../stores/draftGuard";
import { featureTreeRows } from "../../adapters/featureTree";
import FeatureInspection from "./FeatureInspection";
import ProjectMembers from "./ProjectMembers";
import InstanceProperties from "./InstanceProperties";
import DocumentBranches from "./DocumentBranches";
import SketchEditor from "./SketchEditor";
import EngineeringTasks from './EngineeringTasks';
import DocumentReleases from './DocumentReleases';
import LocalBridgePanel from './LocalBridgePanel';
import { useFeatureLease } from "../../hooks/useFeatureLease";
import type { CloudDocumentConnection } from "../../hooks/useCloudDocument";
import type { CloudDocument, DocumentOperation, SemanticFeature } from "../../types/document";
import type { EngineeringTaskSummary } from "../../types/engineeringTask";
import { confirmDurableTask, commentOnDocument, updateDocumentParameters, createDocumentReviewLink, annotateDocumentFeature, EngineeringApiError } from "../../services/engineeringService";

const STATUS: Record<string, string> = { queued: "排队中", running: "运行中", reviewable: "等待审核",
  committed: "已提交", rejected: "已拒绝", failed: "失败", cancelled: "已取消", rolled_back: "已回退" };

function FeatureMeaning({ feature, document }: { feature: SemanticFeature; document: CloudDocument }) {
  const [role, setRole] = useState(feature.role || "");
  const [intent, setIntent] = useState(feature.intent || "");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [annotationId, setAnnotationId] = useState(() => crypto.randomUUID());
  const changed = role !== (feature.role || "") || intent !== (feature.intent || "");
  const save = async () => {
    setPending(true); setError("");
    try { await annotateDocumentFeature(document, feature, role, intent, annotationId); }
    catch (e) { setError(e instanceof Error ? e.message : "标注保存失败"); }
    finally { setPending(false); }
  };
  return <div className="ww-inspector-section">
    <h3>用途与设计意图</h3>
    <p className="type-caption text-[var(--muted)]">由成员填写，供后续修改和 Agent 规划参考。标注版本 {feature.annotation_version || 0}</p>
    <label className="my-2 block type-caption">特征用途<input aria-label="特征用途" maxLength={120} className="mt-1 w-full rounded border border-[var(--line)] p-2"
      value={role} disabled={!document.can_edit || pending} onChange={(e) => { setRole(e.target.value); setAnnotationId(crypto.randomUUID()); }} /></label>
    <label className="my-2 block type-caption">设计意图<textarea aria-label="特征设计意图" maxLength={2000} className="mt-1 w-full rounded border border-[var(--line)] p-2"
      value={intent} disabled={!document.can_edit || pending} onChange={(e) => { setIntent(e.target.value); setAnnotationId(crypto.randomUUID()); }} /></label>
    {document.can_edit ? <button className="workspace-button" type="button" disabled={!changed || pending} onClick={() => void save()}>{pending ? "保存中" : "保存特征标注"}</button> : null}
    {error ? <p role="alert" className="mt-2 type-caption text-red-700">{error}</p> : null}
  </div>;
}

function FeatureProperties({ feature, document, onSubmitted, onReview, operations = [] }: {
  feature: SemanticFeature; document: CloudDocument; onSubmitted: (id: string, document: CloudDocument) => void; operations?:DocumentOperation[];
  onReview?: (id: string) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [editBase, setEditBase] = useState<CloudDocument | null>(null);
  const lease = useFeatureLease(document, feature.id);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const [submittedId, setSubmittedId] = useState<string | null>(null);
  const [attemptToken, setAttemptToken] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const durable = useSessionStore(state=>state.getActivePanel().durable);
  const confirmation = durable?.workflowRunId === submittedId && durable.taskStatus === 'waiting_confirmation' ? durable.confirmation : null;
  const [confirming, setConfirming] = useState(false);
  const confirm = async (accepted:boolean) => {
    if (!confirmation || confirming) return;
    setConfirming(true); setError("");
    try { await confirmDurableTask(confirmation.workflow_run_id, accepted, accepted ? '' : '用户拒绝当前参数执行计划'); }
    catch(e){ setError(e instanceof Error ? e.message : "执行计划确认失败"); }
    finally {setConfirming(false);}
  };
  const [savedMessage, setSavedMessage] = useState("");
  const {operation: submittedOperation, saved, failed} = parameterSubmissionState(submittedId, operations, document);
  // Update only from this request's authoritative operation + corresponding head,
  // never from a different task succeeding or from a changed parameter value.
  if (submittedId && (saved || failed)) {
    setSubmittedId(null); setAttemptToken(null); setUncertain(false); setIdempotencyKey(crypto.randomUUID());
    if (saved) {
      setValues({}); setEditBase(null); setError(""); setSavedMessage(`已保存 · v${document.state_version}，可继续编辑`);
    } else {
      setError(`本次参数修改${STATUS[submittedOperation!.status] || submittedOperation!.status}，输入已保留，可修正后重新提交。${submittedOperation?.error_code || ""}`);
    }
  }
  const originalFeature = editBase?.features.find((f) => f.id === feature.id) || feature;
  const stale = Boolean(editBase && (editBase.head_revision_id !== document.head_revision_id || editBase.state_version !== document.state_version));
  const updates = originalFeature.parameters.filter((p) => p.editable && values[p.id] !== undefined && Number(values[p.id]) !== p.value)
    .map((p) => ({ parameter_id: p.id, value: Number(values[p.id]) }));
  const valid = updates.length > 0 && originalFeature.parameters.every(p => values[p.id] === undefined || (
    !!values[p.id].trim() && Number.isFinite(Number(values[p.id]))
    && (p.minimum === null || Number(values[p.id]) >= p.minimum)
    && (p.maximum === null || Number(values[p.id]) <= p.maximum)));
  const dirty = Object.keys(values).length > 0 && !submittedId;
  const reset = () => { setValues({}); setEditBase(null); setSubmittedId(null); setAttemptToken(null); setUncertain(false); setError(""); setIdempotencyKey(crypto.randomUUID()); void lease.release().catch(() => {}); };
  useDraftGuard(dirty || pending, `${feature.label} 参数`, reset);
  const submit = async () => {
    if (!valid || (stale && !uncertain) || submittedId || !document.can_edit) return;
    setPending(true); setError(""); setSavedMessage("");
    try {
      const token = attemptToken || await lease.ensure();
      setAttemptToken(token);
      const task = await updateDocumentParameters(editBase || document, updates, idempotencyKey, token);
      setSubmittedId(task.workflow_run_id); setUncertain(false);
      onSubmitted(task.workflow_run_id, editBase || document);
      void lease.release().catch(() => {});
    } catch (e) {
      const definitive = e instanceof EngineeringApiError && e.httpStatus !== undefined && e.httpStatus < 500;
      if (definitive) setAttemptToken(null);
      setUncertain(!definitive); setError(e instanceof Error ? e.message : "修改提交失败");
    }
    finally { setPending(false); }
  };
  return <div className="ww-inspector-section" data-i18n-skip data-feature-properties={feature.id}>
    <h3>{feature.label}</h3><p className="type-caption text-[var(--muted)]">{feature.type}</p><details className="type-caption"><summary>对象详情</summary><p>{feature.kernel_name} · {feature.id}</p></details>
    <p className="type-caption">{feature.is_valid === true ? "几何对象有效" : feature.is_valid === false ? "几何对象无效" : "未提供几何检查"}</p>
    {feature.shape?.volume !== undefined && feature.shape.volume > 0 ? <p className="type-caption">体积 {feature.shape.volume.toFixed(2)} mm³</p> : null}
    {originalFeature.parameters.map((p) => <label className="my-3 flex items-center gap-2 type-caption" key={p.id}>
      <span className="min-w-0 flex-1 break-all">{p.property_name} {p.unit || ""}</span>
      <input aria-label={p.id} className="w-24 rounded border border-[var(--line)] bg-white p-2" type="number"
        disabled={!document.can_edit || !p.editable || pending || !!submittedId || uncertain} min={p.minimum ?? undefined} max={p.maximum ?? undefined} step={p.step ?? "any"}
        onFocus={() => { void lease.ensure().catch(() => {}); }}
        value={values[p.id] ?? String(p.value)} onChange={(e) => { setSavedMessage(""); setEditBase((base) => base || document); setValues((v) => ({ ...v, [p.id]: e.target.value })); setIdempotencyKey(crypto.randomUUID()); }} />
    </label>)}
    {feature.parameters.some((p) => p.editable) ? <button className="workspace-button" disabled={!valid || pending || !document.can_edit || (stale && !uncertain) || !!submittedId} onClick={() => void submit()} type="button">{pending ? "正在提交" : uncertain ? "核对并重试同一请求" : submittedId ? "已提交候选计算" : "提交参数变更"}</button> : <p className="type-caption text-[var(--muted)]">{feature.type==='Sketcher::SketchObject' ? '草图尺寸可在下方约束编辑器中修改。' : '此特征没有已开放的可编辑参数。'}</p>}
    {editBase ? <p className="mt-2 type-caption"><strong>{submittedId ? '已提交计算的参数' : '我的未提交修改'}</strong> · 基线 v{editBase.state_version} · {editBase.head_revision_id.slice(0, 8)}。数值经求解、审核并提交后生效。其他任务运行时先保留草稿；提交由服务端排队或报告冲突，不会静默覆盖。</p> : null}
    {submittedOperation ? <p role="status" className="mt-2 type-caption">本次参数任务：{STATUS[submittedOperation.status] || submittedOperation.status}</p> : null}
    {confirmation ? <section aria-label="参数执行计划确认" className="mt-2 type-caption"><p>{confirmation.reason}</p><p>{confirmation.affected_objects.map(item=>`${item.label}（${item.change}）`).join('、')}</p><button className="workspace-button" type="button" disabled={confirming || !document.can_edit} onClick={()=>void confirm(false)}>拒绝执行计划</button><button className="workspace-button" type="button" disabled={confirming || !document.can_edit} onClick={()=>void confirm(true)}>确认并继续</button></section> : null}
    {submittedId ? <div className="mt-2 type-caption" role="status">
      {submittedOperation?.change_set_id && ['reviewable','committed'].includes(submittedOperation.status)
        ? <><p>{submittedOperation.status === 'committed' ? '候选已提交，正在同步已保存参数…' : '计算完成，审核后应用修改。'}</p>
          {onReview ? <button className="workspace-button mt-2" type="button" onClick={()=>onReview(submittedOperation.change_set_id!)}>查看变更 / 应用修改</button> : null}</>
        : <p>{confirmation ? "等待确认执行计划" : "计算中 · 保留本次输入，完成后在此查看变更。"}</p>}
      <details><summary>请求详情</summary>{submittedId}</details>
    </div> : null}
    {savedMessage ? <p role="status" className="mt-2 type-caption text-emerald-700">{savedMessage}</p> : null}
    {dirty && !valid ? <p role="status" className="mt-2 type-caption">请输入有变化、非空且位于允许范围内的有限数值。</p> : null}
    {uncertain ? <p role="status" className="mt-2 type-caption">尚未确认服务器响应。重试会沿用同一请求 ID 和原始数值，避免重复建模。</p> : null}
    {editBase && !pending && !submittedId ? <button className="workspace-button mt-2" type="button" onClick={() => guardDraft(reset)}>放弃草稿并读取当前参数</button> : null}
    {error ? <p role="alert" className="mt-2 type-caption text-red-700">{error}</p> : null}
    {lease.error ? <p role="alert" className="mt-2 type-caption text-red-700">{lease.error}</p> : null}
    {lease.held ? <p className="mt-2 type-caption">已保留此特征的编辑租约。<button className="underline" type="button" onClick={() => void lease.release().catch((e: unknown) => setError(e instanceof Error ? e.message : "释放失败"))}>释放租约</button></p> : null}
    {stale ? <p role="status" className="mt-2 type-caption">当前版本已提交或回退，草稿基线过期。请读取最新参数后重新确认；系统不会自动换基线执行。</p> : null}
    {feature.dependencies.length ? <p className="mt-3 type-caption">依赖：{feature.dependencies.map((id) => document.features.find((f) => f.id === id)?.label || id).join("、")}</p> : null}
  </div>;
}

export type DocumentSection = "features" | "activity" | "sharing" | "versions" | "engineering";

export default function CloudDocumentPanel({ connection, onSubmitted, onReview, onTask, onEngineeringTasksChange, section }: {
  section?: DocumentSection;
  connection: CloudDocumentConnection; onSubmitted?: (id: string, document: CloudDocument) => void;
  onReview?: (id: string) => void;
  onTask?: (id: string) => void;
  onEngineeringTasksChange?: (documentId: string, tasks: EngineeringTaskSummary[]) => void;
}) {
  const { document, collaboration, selectedId, select, connected, error } = connection;
  const treeRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    treeRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({block:'nearest'});
  }, [selectedId]);
  const [localSection,setLocalSection]=useState<DocumentSection | null>(null);
  const [body, setBody] = useState("");
  const [commentError, setCommentError] = useState("");
  const [saving, setSaving] = useState(false);
  const [reviewLink, setReviewLink] = useState("");
  const [sharing, setSharing] = useState(false);
  const [inviteRole, setInviteRole] = useState<"viewer" | "editor">("viewer");
  const [grantedRole, setGrantedRole] = useState<"viewer" | "editor">("viewer");
  if (!document) return <p className="ww-inspector-empty" role="status">{error || "正在读取云文档…"}</p>;
  const currentSection=section || localSection || (document.fcstd ? "features" : "activity");
  const tree = featureTreeRows(document.features, document.hierarchy_status, document.roots);
  const selected = document.features.find((f) => f.id === selectedId);
  const share = async () => {
    setSharing(true); setCommentError("");
    try { setReviewLink(await createDocumentReviewLink(document.document_id, inviteRole)); setGrantedRole(inviteRole); }
    catch (e) { setCommentError(e instanceof Error ? e.message : "邀请创建失败"); }
    finally { setSharing(false); }
  };
  const addComment = async () => {
    setSaving(true); setCommentError("");
    try { await commentOnDocument(document, body.trim(), selectedId); setBody(""); }
    catch (e) { setCommentError(e instanceof Error ? e.message : "评论保存失败"); }
    finally { setSaving(false); }
  };
  return <div data-testid="cloud-document-panel">
    {!section ? <div className="ww-inspector-tabs" role="tablist" aria-label="云文档内容">{([
      ["features","特征与属性"],["activity","需求与任务"],["sharing","共享"],["versions","版本"],["engineering","工程"]
    ] as const).map(([id,label])=><button role="tab" aria-selected={currentSection===id} className={currentSection===id?"is-active":""} key={id} type="button" onClick={()=>guardDraft(()=>setLocalSection(id))}>{label}</button>)}</div> : null}
    <details className="ww-inspector-section" open={currentSection !== "features"}><summary>文档详情</summary>
      <h3>云文档 <span className="type-caption text-[var(--muted)]">{!document.view_mode || document.view_mode === "committed" ? `v${document.state_version}` : `${document.view_mode === "candidate" ? "候选" : "历史"} ${document.revision_id.slice(0, 8)}`}</span></h3>
      <p className="type-caption text-[var(--muted)]">{connected ? "文档已同步" : "文档连接中断"} · {collaboration?.presence.length ?? 0} 个在线会话</p>
      <p className="mt-1 type-caption">{document.view_mode && document.view_mode !== "committed" ? "当前查看版本只读；请返回已提交版本后编辑。" : document.modeling_backend ? "已保存版本 · 手动编辑先保留为草稿；AI 方案审核并提交后才更新此版本。" : "空文档，尚未保存模型。首个候选审核并提交后成为当前版本。"}</p>
      {currentSection==="sharing" && document.can_share && onSubmitted ? <div className="mt-2"><select aria-label="邀请权限" className="mr-2 rounded border border-[var(--line)] p-2 type-caption" value={inviteRole} onChange={(e) => setInviteRole(e.target.value as "viewer" | "editor")}><option value="viewer">审阅者：查看和评论</option><option value="editor">编辑者：修改和审阅</option></select><button className="workspace-button mt-2" type="button" disabled={sharing} onClick={() => void share()}>{inviteRole === "viewer" ? "邀请项目审阅者" : "邀请项目编辑者"}</button></div> : null}
      {currentSection==="sharing" && reviewLink ? <div className="mt-2 type-caption"><p>链接 24 小时有效，仅可由一个账号接受，{grantedRole === "viewer" ? "授予本项目查看和评论权限。" : "授予本项目编辑和审阅权限；版本提交仍由项目管理者执行。"}</p><input aria-label="项目审阅邀请链接" className="mt-1 w-full rounded border border-[var(--line)] p-2" readOnly value={reviewLink} onFocus={(e) => e.target.select()} /></div> : null}
    </details>
    {error ? <p role="alert" className="ww-inspector-section type-caption text-red-700">{error}</p> : null}
    {currentSection==="sharing" && document.can_share ? <ProjectMembers key={document.document_id} documentId={document.document_id} /> : null}
    {currentSection==="versions" ? <DocumentBranches key={`branches:${document.document_id}`} document={document} onSubmitted={onSubmitted} /> : null}
    {currentSection==="engineering" ? <EngineeringTasks key={`engineering:${document.document_id}`} document={document} onTasksChange={onEngineeringTasksChange} /> : null}
    {currentSection==="versions" ? <DocumentReleases key={`releases:${document.document_id}`} document={document} /> : null}
    {currentSection==="versions" ? <LocalBridgePanel key={`bridge:${document.document_id}`} document={document} /> : null}
    {currentSection==="features" ? <>
    <div className="ww-inspector-section ww-feature-tree" ref={treeRef} role="tree" aria-label="模型特征树" data-i18n-skip>
      {!tree.hierarchyAvailable && document.features.length ? <p className="mb-2 type-caption text-[var(--muted)]">此版本未提供完整容器层级，按特征列表显示；依赖关系见特征详情。</p> : null}
      {tree.rows.map(({feature,level,bodyTip}) => <button key={feature.id} role="treeitem" aria-label={feature.label} aria-level={level} aria-selected={feature.id === selectedId}
        style={{paddingLeft: 12 + (level - 1) * 16}}
        className={`mb-1 flex w-full items-center gap-2 rounded border px-3 py-2 text-left type-caption ${feature.id === selectedId ? "border-[var(--agent)] bg-[var(--agent-soft)]" : "border-transparent hover:bg-[var(--surface)]"}`}
        onClick={() => select(feature.id)} type="button"><span aria-hidden>{feature.structure?.member_ids.length ? "▾" : feature.structure?.category === "datum" ? "○" : "◇"}</span><span className="min-w-0 truncate">{feature.label}</span>{bodyTip ? <small aria-hidden className="ml-auto text-[var(--muted)]">Body Tip</small> : null}</button>)}
      {!document.features.length ? <p className="type-caption text-[var(--muted)]">{document.modeling_backend ? "当前模型没有原生特征状态。" : "提交首个模型后显示特征树。"}</p> : null}
    </div>
    {!selected ? <p className="ww-inspector-section type-caption">选择模型对象或特征，查看参数与约束。</p> : null}
    {selected && onSubmitted ? <FeatureProperties key={`${document.document_id}:${selected.id}`} feature={selected} document={document} operations={collaboration?.operations} onSubmitted={onSubmitted} onReview={onReview} /> : selected ? <div className="ww-inspector-section" data-i18n-skip><h3>{selected.label}</h3><p className="type-caption">{selected.type}</p>{selected.parameters.map((p) => <p key={p.id} className="type-caption">{p.property_name}: {p.value} {p.unit}</p>)}</div> : null}
    {collaboration?.leases?.length ? <div className="ww-inspector-section"><h3>正在编辑</h3>{collaboration.leases.map((l) => <p className="type-caption" key={l.feature_id}>{document.features.find((f) => f.id === l.feature_id)?.label || l.feature_id} · {l.display_name || "项目成员"}</p>)}</div> : null}
    {selected ? <details><summary className="ww-inspector-section">特征说明</summary><FeatureMeaning key={`meaning:${document.head_revision_id}:${selected.id}:${selected.annotation_version || 0}`} feature={selected} document={document} /></details> : null}
    {selected?.type === 'Sketcher::SketchObject' ? <SketchEditor key={`sketch:${document.document_id}:${selected.id}`} document={document} feature={selected} onSubmitted={onSubmitted} /> : null}
    {selected && onSubmitted && ['PartDesign::Body','Part::Feature','PartDesign::Feature','App::Link'].includes(selected.type || '') ? <InstanceProperties
      key={`instance:${document.head_revision_id}:${selected.id}`} document={document} feature={selected} onSubmitted={onSubmitted} /> : null}
    {selected ? <FeatureInspection key={`inspection:${document.revision_id}:${selected.id}`} feature={selected} document={document} /> : null}
    </> : null}
    {currentSection==="activity" ? <div className="ww-inspector-section"><h3>操作记录</h3>
      {collaboration?.operations.map((op) => <div className="my-3 border-b border-[var(--line)] pb-3 type-caption" key={op.id}>
        <p data-i18n-skip className="break-words">{op.objective || op.action}</p><span>{STATUS[op.status] || op.status}</span>
        {op.started_at && op.finished_at ? <span> · {Math.max(0, (Date.parse(op.finished_at) - Date.parse(op.started_at)) / 1000).toFixed(1)} 秒</span> : null}
        {op.error_code ? <details><summary>本次操作失败 · 查看详情</summary><code>{op.error_code}</code></details> : null}
        {op.change_set_id && onReview ? <button className="workspace-button mt-2" onClick={() => onReview(op.change_set_id!)} type="button">查看变更</button> : null}
        {onTask ? <button className="workspace-button mt-2" onClick={() => onTask(op.id)} type="button">查看任务</button> : null}
      </div>)}
      {!collaboration?.operations.length ? <p className="type-caption text-[var(--muted)]">尚无文档操作。</p> : null}
    </div> : null}
    {currentSection==="sharing" ? <div className="ww-inspector-section"><h3>版本评论</h3>
      <p className="type-caption text-[var(--muted)]">{selected ? `标注特征：${selected.label}` : "标注当前版本"}</p>
      <textarea aria-label="文档评论" className="my-2 w-full rounded border border-[var(--line)] p-2 type-caption" maxLength={4000} value={body} onChange={(e) => setBody(e.target.value)} />
      <button className="workspace-button" disabled={!body.trim() || saving || !connected} onClick={() => void addComment()} type="button">{saving ? "保存中" : "保存评论"}</button>
      {commentError ? <p role="alert" className="type-caption text-red-700">{commentError}</p> : null}
      {collaboration?.comments.map((comment) => <div className="my-3 border-b border-[var(--line)] pb-3 type-caption" key={comment.id} data-i18n-skip>
        <p className="whitespace-pre-wrap break-words">{comment.body}</p><small className="text-[var(--muted)]">{comment.display_name || comment.principal_id.slice(0, 8)} · 版本 {comment.revision_id.slice(0, 8)}</small>
      </div>)}
    </div> : null}
  </div>;
}
