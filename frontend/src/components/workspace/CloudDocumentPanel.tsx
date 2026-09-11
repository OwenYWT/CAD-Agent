import { useState } from "react";
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
import type { CloudDocument, SemanticFeature } from "../../types/document";
import type { EngineeringTaskSummary } from "../../types/engineeringTask";
import { commentOnDocument, updateDocumentParameters, createDocumentReviewLink, annotateDocumentFeature } from "../../services/engineeringService";

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

function FeatureProperties({ feature, document, onSubmitted }: {
  feature: SemanticFeature; document: CloudDocument; onSubmitted: (id: string, document: CloudDocument) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [editBase, setEditBase] = useState<CloudDocument | null>(null);
  const lease = useFeatureLease(document, feature.id);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const originalFeature = editBase?.features.find((f) => f.id === feature.id) || feature;
  const updates = originalFeature.parameters.filter((p) => p.editable && values[p.id] !== undefined && Number(values[p.id]) !== p.value)
    .map((p) => ({ parameter_id: p.id, value: Number(values[p.id]) }));
  const valid = updates.length > 0 && updates.every((p) => values[p.parameter_id]?.trim() && Number.isFinite(p.value));
  const submit = async () => {
    setPending(true); setError("");
    try {
      const token = await lease.ensure();
      const task = await updateDocumentParameters(editBase || document, updates, idempotencyKey, token);
      onSubmitted(task.workflow_run_id, document);
      setValues({}); setEditBase(null); setIdempotencyKey(crypto.randomUUID());
      await lease.release();
    } catch (e) { setError(e instanceof Error ? e.message : "修改提交失败"); }
    finally { setPending(false); }
  };
  return <div className="ww-inspector-section" data-i18n-skip>
    <h3>{feature.label}</h3><p className="type-caption text-[var(--muted)]">{feature.type} · {feature.kernel_name}</p>
    <p className="type-caption">{feature.is_valid === true ? "几何对象有效" : feature.is_valid === false ? "几何对象无效" : "未提供几何检查"}</p>
    {feature.shape?.volume !== undefined && feature.shape.volume > 0 ? <p className="type-caption">体积 {feature.shape.volume.toFixed(2)} mm³</p> : null}
    {feature.parameters.map((p) => <label className="my-3 flex items-center gap-2 type-caption" key={p.id}>
      <span className="min-w-0 flex-1 break-all">{p.property_name} {p.unit || ""}</span>
      <input aria-label={p.id} className="w-24 rounded border border-[var(--line)] bg-white p-2" type="number"
        disabled={!document.can_edit || !p.editable || pending} min={p.minimum ?? undefined} max={p.maximum ?? undefined} step={p.step ?? "any"}
        onFocus={() => { void lease.ensure().catch(() => {}); }}
        value={values[p.id] ?? String(p.value)} onChange={(e) => { setEditBase((base) => base || document); setValues((v) => ({ ...v, [p.id]: e.target.value })); setIdempotencyKey(crypto.randomUUID()); }} />
    </label>)}
    {feature.parameters.some((p) => p.editable) ? <button className="workspace-button" disabled={!valid || pending || !document.can_edit} onClick={() => void submit()} type="button">{pending ? "正在提交" : "提交参数变更"}</button> : <p className="type-caption text-[var(--muted)]">{feature.type==='Sketcher::SketchObject' ? '草图尺寸可在下方约束编辑器中修改。' : '此特征没有已开放的可编辑参数。'}</p>}
    {error ? <p role="alert" className="mt-2 type-caption text-red-700">{error}</p> : null}
    {lease.error ? <p role="alert" className="mt-2 type-caption text-red-700">{lease.error}</p> : null}
    {lease.held ? <p className="mt-2 type-caption">已保留此特征的编辑租约。<button className="underline" type="button" onClick={() => void lease.release().catch((e: unknown) => setError(e instanceof Error ? e.message : "释放失败"))}>释放租约</button></p> : null}
    {editBase && editBase.head_revision_id !== document.head_revision_id ? <p className="mt-2 type-caption">其他成员已提交新版本。提交时会检查依赖，只有互不影响的参数变更才会在新版本上重新执行并送审。</p> : null}
    {feature.dependencies.length ? <p className="mt-3 type-caption">依赖：{feature.dependencies.map((id) => document.features.find((f) => f.id === id)?.label || id).join("、")}</p> : null}
  </div>;
}

export default function CloudDocumentPanel({ connection, onSubmitted, onReview, onTask, onEngineeringTasksChange }: {
  connection: CloudDocumentConnection; onSubmitted?: (id: string, document: CloudDocument) => void;
  onReview?: (id: string) => void;
  onTask?: (id: string) => void;
  onEngineeringTasksChange?: (documentId: string, tasks: EngineeringTaskSummary[]) => void;
}) {
  const { document, collaboration, selectedId, select, connected, error } = connection;
  const [body, setBody] = useState("");
  const [commentError, setCommentError] = useState("");
  const [saving, setSaving] = useState(false);
  const [reviewLink, setReviewLink] = useState("");
  const [sharing, setSharing] = useState(false);
  const [inviteRole, setInviteRole] = useState<"viewer" | "editor">("viewer");
  const [grantedRole, setGrantedRole] = useState<"viewer" | "editor">("viewer");
  if (!document) return <p className="ww-inspector-empty" role="status">{error || "正在读取云文档…"}</p>;
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
    <div className="ww-inspector-section">
      <h3>云文档 <span className="type-caption text-[var(--muted)]">v{document.state_version}</span></h3>
      <p className="type-caption text-[var(--muted)]">{connected ? "已同步" : "连接中断"} · {collaboration?.presence.length ?? 0} 个在线会话</p>
      <p className="mt-1 type-caption">特征和参数来自已提交版本；变更审核通过并提交后更新。</p>
      {document.can_share && onSubmitted ? <div className="mt-2"><select aria-label="邀请权限" className="mr-2 rounded border border-[var(--line)] p-2 type-caption" value={inviteRole} onChange={(e) => setInviteRole(e.target.value as "viewer" | "editor")}><option value="viewer">审阅者：查看和评论</option><option value="editor">编辑者：修改和审阅</option></select><button className="workspace-button mt-2" type="button" disabled={sharing} onClick={() => void share()}>{inviteRole === "viewer" ? "邀请项目审阅者" : "邀请项目编辑者"}</button></div> : null}
      {reviewLink ? <div className="mt-2 type-caption"><p>链接 24 小时有效，仅可由一个账号接受，{grantedRole === "viewer" ? "授予本项目查看和评论权限。" : "授予本项目编辑和审阅权限；版本提交仍由项目管理者执行。"}</p><input aria-label="项目审阅邀请链接" className="mt-1 w-full rounded border border-[var(--line)] p-2" readOnly value={reviewLink} onFocus={(e) => e.target.select()} /></div> : null}
      {error ? <p role="alert" className="type-caption text-red-700">{error}</p> : null}
    </div>
    {document.can_share ? <ProjectMembers key={document.document_id} documentId={document.document_id} /> : null}
    <DocumentBranches key={`branches:${document.document_id}`} document={document} onSubmitted={onSubmitted} />
    <EngineeringTasks key={`engineering:${document.document_id}`} document={document} onTasksChange={onEngineeringTasksChange} />
    <DocumentReleases key={`releases:${document.document_id}`} document={document} />
    <LocalBridgePanel key={`bridge:${document.document_id}`} document={document} />
    <div className="ww-inspector-section" role="tree" aria-label="模型特征树" data-i18n-skip>
      {document.features.filter((feature) => feature.kernel_name !== 'CADAgentLedger').map((feature) => <button key={feature.id} role="treeitem" aria-selected={feature.id === selectedId}
        className={`mb-1 flex w-full items-center gap-2 rounded border px-3 py-2 text-left type-caption ${feature.id === selectedId ? "border-[var(--agent)] bg-[var(--agent-soft)]" : "border-transparent hover:bg-[var(--surface)]"}`}
        onClick={() => select(feature.id)} type="button"><span aria-hidden>◇</span><span className="min-w-0 truncate">{feature.label}</span></button>)}
      {!document.features.length ? <p className="type-caption text-[var(--muted)]">{document.modeling_backend ? "当前模型没有原生特征状态。" : "提交首个模型后显示特征树。"}</p> : null}
    </div>
    {selected && onSubmitted ? <FeatureProperties key={`${document.document_id}:${selected.id}`} feature={selected} document={document} onSubmitted={onSubmitted} /> : selected ? <div className="ww-inspector-section" data-i18n-skip><h3>{selected.label}</h3><p className="type-caption">{selected.type}</p>{selected.parameters.map((p) => <p key={p.id} className="type-caption">{p.property_name}: {p.value} {p.unit}</p>)}</div> : null}
    {collaboration?.leases?.length ? <div className="ww-inspector-section"><h3>正在编辑</h3>{collaboration.leases.map((l) => <p className="type-caption" key={l.feature_id}>{document.features.find((f) => f.id === l.feature_id)?.label || l.feature_id} · {l.display_name || "项目成员"}</p>)}</div> : null}
    {selected ? <FeatureMeaning key={`meaning:${document.head_revision_id}:${selected.id}:${selected.annotation_version || 0}`} feature={selected} document={document} /> : null}
    {selected?.type === 'Sketcher::SketchObject' ? <SketchEditor key={`sketch:${document.document_id}:${selected.id}`} document={document} feature={selected} onSubmitted={onSubmitted} /> : null}
    {selected && onSubmitted && ['PartDesign::Body','Part::Feature','PartDesign::Feature','App::Link'].includes(selected.type || '') ? <InstanceProperties
      key={`instance:${document.head_revision_id}:${selected.id}`} document={document} feature={selected} onSubmitted={onSubmitted} /> : null}
    {selected ? <FeatureInspection key={`inspection:${document.head_revision_id}:${selected.id}`} feature={selected} document={document} /> : null}
    <div className="ww-inspector-section"><h3>操作记录</h3>
      {collaboration?.operations.map((op) => <div className="my-3 border-b border-[var(--line)] pb-3 type-caption" key={op.id}>
        <p data-i18n-skip className="break-words">{op.objective || op.action}</p><span>{STATUS[op.status] || op.status}</span>
        {op.started_at && op.finished_at ? <span> · {Math.max(0, (Date.parse(op.finished_at) - Date.parse(op.started_at)) / 1000).toFixed(1)} 秒</span> : null}
        {op.error_code ? <p role="alert" className="text-red-700">{op.error_code}</p> : null}
        {op.change_set_id && onReview ? <button className="workspace-button mt-2" onClick={() => onReview(op.change_set_id!)} type="button">查看变更</button> : null}
        {onTask ? <button className="workspace-button mt-2" onClick={() => onTask(op.id)} type="button">查看任务</button> : null}
      </div>)}
      {!collaboration?.operations.length ? <p className="type-caption text-[var(--muted)]">尚无文档操作。</p> : null}
    </div>
    <div className="ww-inspector-section"><h3>版本评论</h3>
      <p className="type-caption text-[var(--muted)]">{selected ? `标注特征：${selected.label}` : "标注当前版本"}</p>
      <textarea aria-label="文档评论" className="my-2 w-full rounded border border-[var(--line)] p-2 type-caption" maxLength={4000} value={body} onChange={(e) => setBody(e.target.value)} />
      <button className="workspace-button" disabled={!body.trim() || saving || !connected} onClick={() => void addComment()} type="button">{saving ? "保存中" : "保存评论"}</button>
      {commentError ? <p role="alert" className="type-caption text-red-700">{commentError}</p> : null}
      {collaboration?.comments.map((comment) => <div className="my-3 border-b border-[var(--line)] pb-3 type-caption" key={comment.id} data-i18n-skip>
        <p className="whitespace-pre-wrap break-words">{comment.body}</p><small className="text-[var(--muted)]">{comment.display_name || comment.principal_id.slice(0, 8)} · 版本 {comment.revision_id.slice(0, 8)}</small>
      </div>)}
    </div>
  </div>;
}
