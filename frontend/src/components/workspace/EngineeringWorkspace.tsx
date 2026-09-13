import { RequirementSummary } from "../agent/RequirementCard";
import type { InspectorTab } from "./WorkspaceInspector";
import type { RequirementBasis } from "../../types/requirements";
import { taskState } from "../../adapters/taskState";
import { retryDurableTask } from "../../services/engineeringService";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { AuthUser } from "../../auth";
import EngineeringTasks from "./EngineeringTasks";
import type { EngineeringTaskSummary } from "../../types/engineeringTask";
import { adaptEngineeringProject } from "../../adapters/projectAdapter";
import {
  hydrateGenerationResult,
  resultMatchesSnapshot,
} from "../../adapters/resultSnapshotAdapter";
import { useWebSocket } from "../../hooks/useWebSocket";
import {
  getModelSnapshot,
  listEngineeringTasks,
  listModelSnapshots,
} from "../../services/engineeringService";
import { useSessionStore } from "../../stores/sessionStore";
import type {
  DesignAnalysis,
  ManufacturingProfile,
  ModelSnapshotDetail,
} from "../../types";
import type {
  DurableChangeSetDetail,
  EngineeringDomain,
  EngineeringStage,
} from "../../types/engineering";
import AgentDrawer from "../agent/AgentDrawer";
import AgentPanel from "../agent/AgentPanel";
import ChangeSetDialog from "../changes/ChangeSetDialog";
import ExportDialog from "../export/ExportDialog";
import ParameterDrawer from "../parameters/ParameterDrawer";
import ProjectFlow from "../project/ProjectFlow";
import ProjectSidebar from "../project/ProjectSidebar";
import ProjectStart from "../project/ProjectStart";
import WorkspaceHeader from "../project/WorkspaceHeader";
import SettingsDrawer from "../SettingsDrawer";
import VersionHistoryPanel from "../VersionHistoryPanel";
import ValidationDialog from "../validation/ValidationDialog";
import MechanicalWorkspace from "../viewer/MechanicalWorkspace";
import { Icon } from "../ui/Icon";
import { LanguageSwitch } from "../../i18n/LanguageSwitch";
import WorkspaceShell from "./WorkspaceShell";
import WorkspaceInspector from "./WorkspaceInspector";
import CloudDocumentPanel from "./CloudDocumentPanel";
import { useCloudDocument } from "../../hooks/useCloudDocument";
import type { CloudDocument } from "../../types/document";
import { useDocumentView } from "../../hooks/useDocumentView";
import { viewLabel } from "../../adapters/documentView";
import { guardDraft, useDraftGuardStore } from "../../stores/draftGuard";

interface EngineeringWorkspaceProps {
  user: AuthUser;
  onLogout: () => void;
  onUserUpdate: (user: AuthUser) => void;
}

export default function EngineeringWorkspace({ user, onLogout, onUserUpdate }: EngineeringWorkspaceProps) {
  const ownerId = useSessionStore((state) => state.ownerId);
  const bindOwner = useSessionStore((state) => state.bindOwner);
  const sessionId = useSessionStore((state) => state.sessionId);
  const panel = useSessionStore((state) => state.getActivePanel());
  const cloud = useCloudDocument(panel.durable?.branchId || null);
  const task = useMemo(() => taskState(panel,cloud.document), [panel, cloud.document]);
  const [reviewDocumentChange, setReviewDocumentChange] = useState<string | null>(null);
  const onDocumentSubmitted = (id: string, document: CloudDocument) => {
    const current = useSessionStore.getState();
    if (current.ownerId !== ownerId || current.sessionId !== sessionId) return;
    useSessionStore.getState().setDurableWorkflowStarted({ workflow_run_id: id,
      project_id: document.project_id, branch_id: document.active_branch_id,
      expected_base_revision_id: document.head_revision_id, panel_id: panel.id, status: "pending" }, panel.id);
  };
  const applyDurableChangeSet = useSessionStore(
    (state) => state.applyDurableChangeSet,
  );
  const addPanel = useSessionStore((state) => state.addPanel);
  const { connectionState, sendMessage, executeCode, restoreRevision, resumeRun, restoreContext, modifyPart, modifyParameters, cancelGeneration, pendingRequest, retryPendingSubmission } = useWebSocket();
  const [snapshotEvidence, setSnapshotEvidence] = useState<ModelSnapshotDetail | null>(null);
  const [analysisByRequest, setAnalysisByRequest] = useState<Record<string, DesignAnalysis>>({});
  const hydratedPanel = useMemo(() => {
    if (!panel.result || !snapshotEvidence) return panel;
    const result = hydrateGenerationResult(panel.result, snapshotEvidence);
    return result === panel.result ? panel : { ...panel, result };
  }, [panel, snapshotEvidence]);
  const documentView = useDocumentView(cloud.document, cloud.connected, hydratedPanel.result, panel.durable?.changeSetStatus);
  const viewedResult = panel.durable?.branchId ? documentView.result : hydratedPanel.result;
  const viewedPanel = useMemo(() => ({ ...hydratedPanel, result: viewedResult }), [hydratedPanel, viewedResult]);
  const activeAnalysis = viewedResult?.request_id
    ? analysisByRequest[viewedResult.request_id] || null
    : null;
  const [viewSelection, setViewSelection] = useState<{ revision: string; id: string | null } | null>(null);
  const viewIsCommitted = !documentView.identity || documentView.identity.mode === "committed";
  const viewedSelectedId = viewIsCommitted ? cloud.selectedId : viewSelection?.revision === documentView.identity?.viewedRevisionId ? viewSelection?.id || null : null;
  const selectViewedFeature = (id: string | null) => {
    if (viewIsCommitted) cloud.select(id);
    else if (documentView.identity) guardDraft(() => setViewSelection({ revision: documentView.identity!.viewedRevisionId, id }));
  };
  const viewedCloud = { ...cloud, document: documentView.document, selectedId: viewedSelectedId, select: selectViewedFeature,
    error: documentView.error || cloud.error };
  const currentResultVisible = !documentView.identity || hydratedPanel.result?.revision_id === documentView.identity.viewedRevisionId;
  const viewedArtifacts = currentResultVisible ? panel.artifactUpdates : [];
  const viewedAgent = currentResultVisible ? panel.durable?.agent : null;
  const draftCount = useDraftGuardStore(state => Object.keys(state.drafts).length);
  const agentBlockedReason = !viewIsCommitted ? "请先返回已提交版本，再请求 AI 继续修改。"
    : pendingRequest ? "正在确认原请求是否受理，请先查询原请求状态。"
    : draftCount ? "请先提交或放弃手动编辑草稿，再让 AI 修改。"
      : panel.durable?.branchId && !cloud.connected ? "正在同步已提交文档，连接恢复后可继续修改。" : undefined;
  const selectedFeature = cloud.selectionContext ? cloud.document?.features.find(f => f.id === cloud.selectionContext?.feature_ids[0]) : null;
  const selectionLabel = selectedFeature && cloud.selectionContext ? `${selectedFeature.label} · ${selectedFeature.type} · v${cloud.selectionContext.state_version} · ${cloud.selectionContext.revision_id.slice(0, 8)}` : undefined;
  const sendSelectedMessage = (text: string, basis?: RequirementBasis) => {
    if (agentBlockedReason) return false;
    return sendMessage(text, "auto", null, cloud.selectionContext,basis);
  };
  const [engineeringSnapshot, setEngineeringSnapshot] = useState<{documentId:string;tasks:EngineeringTaskSummary[];error?:string} | null>(null);
  const onEngineeringTasksChange = useCallback((documentId:string,tasks:EngineeringTaskSummary[]) => {
    setEngineeringSnapshot({documentId,tasks});
  }, []);
  const engineeringDocumentId = cloud.document?.document_id;
  const engineeringRevisionId = cloud.document?.head_revision_id;
  useEffect(() => {
    if (!engineeringDocumentId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let delay = 10000;
      try {
        const tasks = await listEngineeringTasks(engineeringDocumentId, controller.signal);
        if (controller.signal.aborted) return;
        setEngineeringSnapshot({documentId:engineeringDocumentId,tasks});
        if (tasks.some(task => ["pending","planning","running","cancelling"].includes(task.status))) delay = 2000;
      } catch (error) {
        if (controller.signal.aborted) return;
        setEngineeringSnapshot({documentId:engineeringDocumentId,tasks:[],
          error:error instanceof Error ? error.message : "工程任务读取失败"});
      }
      timer = setTimeout(() => void poll(), delay);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [engineeringDocumentId, engineeringRevisionId]);
  const model = useMemo(
    () => adaptEngineeringProject(sessionId, viewedPanel, activeAnalysis, documentView.document,
      engineeringSnapshot?.documentId === cloud.document?.document_id ? engineeringSnapshot?.tasks : [],
      {pending:engineeringSnapshot?.documentId !== cloud.document?.document_id,
        error:engineeringSnapshot?.documentId === cloud.document?.document_id ? engineeringSnapshot?.error : undefined}, task),
    [activeAnalysis, viewedPanel, sessionId, cloud.document, documentView.document, engineeringSnapshot, task],
  );
  const canExport = (documentView.document?.can_export ?? cloud.document?.can_export ?? true) && model.exports.some(item=>item.status==="available");
  const canEdit = documentView.document?.can_edit ?? true;
  const hasProject = panel.messages.length > 0 || panel.result !== null || panel.isGenerating;
  const [view, setView] = useState<EngineeringDomain>("mechanical");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [agentCollapsed, setAgentCollapsed] = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [inspectorSelection,setInspectorSelection]=useState<{panelId:string;tab:InspectorTab} | null>(null);
  const inspectorTab=inspectorSelection?.panelId===panel.id ? inspectorSelection.tab : documentView.document?.fcstd ? "document" : "requirements";
  const selectInspectorTab=(tab:InspectorTab)=> {
    if (tab !== inspectorTab) guardDraft(() => setInspectorSelection({panelId:panel.id,tab}));
  };
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [mobileSidebar, setMobileSidebar] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [mobilePreviewOpen, setMobilePreviewOpen] = useState(false);
  const [agentSuggestion, setAgentSuggestion] = useState<{
    contextKey: string;
    prompt: string;
    sequence: number;
  } | null>(null);
  const agentContextKey = `${ownerId}:${sessionId}:${panel.id}`;
  const activeSuggestion = agentSuggestion?.contextKey === agentContextKey ? agentSuggestion : null;
  const agentPrompt = activeSuggestion?.prompt || "";
  // Component-local drafts and confirmations must not survive a panel switch.
  // A new explicit suggestion remounts the mobile composer as well as the drawer.
  const agentKey = `${agentContextKey}:${activeSuggestion?.sequence || 0}`;
  const [parametersOpen, setParametersOpen] = useState(false);
  const [checksOpen, setChecksOpen] = useState(false);
  const [changesOpen, setChangesOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);

  useEffect(() => {
    bindOwner(user.id);
  }, [bindOwner, user.id]);

  useEffect(() => {
    let cancelled = false;
    let retryTimer: number | null = null;
    const result = panel.result;

    if (!result?.success || panel.isGenerating) return undefined;
    if (result.snapshot_id && result.parameters?.length) return undefined;

    const loadEvidence = async (attempt: number) => {
      try {
        const snapshots = await listModelSnapshots(panel.id);
        for (const summary of snapshots.slice(0, 6)) {
          const detail = await getModelSnapshot(summary.id);
          if (resultMatchesSnapshot(result, detail)) {
            if (!cancelled) setSnapshotEvidence(detail);
            return;
          }
        }
      } catch {
        // VersionHistoryPanel remains the explicit retry surface if the
        // history service is temporarily unavailable.
      }

      if (!cancelled && attempt < 2) {
        retryTimer = window.setTimeout(
          () => void loadEvidence(attempt + 1),
          500 * 2 ** attempt,
        );
      }
    };

    retryTimer = window.setTimeout(() => void loadEvidence(0), 0);
    return () => {
      cancelled = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
    };
  }, [
    panel.id,
    panel.isGenerating,
    panel.result,
  ]);

  const startProject = (prompt: string, profile: ManufacturingProfile | null = null, basis?: RequirementBasis) => {
    if (!sendMessage(prompt, "auto", profile,undefined,basis)) return false;
    useSessionStore.getState().beginGeneration(undefined,basis);
    useSessionStore.getState().addMessage({ role: "user", content: prompt });
    setView("mechanical");
    return true;
  };

  const navigate = (next: string) => guardDraft(() => setView(next as EngineeringDomain));
  const createProject = () => {
    guardDraft(() => { addPanel(); setView("mechanical"); });
  };
  const openStage = (stage: EngineeringStage) => {
    if (!stage.available) return;
    if (stage.id === "requirements" && model.result?.design_brief) { setChecksOpen(true); return; }
    if (stage.id === "manufacturing") { setChecksOpen(true); return; }
    if (stage.id === "release") { setExportOpen(true); return; }
    if (stage.domain) setView(stage.domain);
  };
  const askAgent = (prompt = "") => {
    setInspectorOpen(false);
    if (prompt) {
      setAgentSuggestion((previous) => ({
        contextKey: agentContextKey,
        prompt,
        sequence: (previous?.sequence || 0) + 1,
      }));
    }
    setAgentCollapsed(false);
    if (window.matchMedia("(max-width: 760px)").matches) {
      setMobilePreviewOpen(false);
      return;
    }
    if (window.matchMedia("(max-width: 899px)").matches) setAgentOpen(true);
  };

  useEffect(() => {
    if (!mobilePreviewOpen) return;
    const closePreview = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobilePreviewOpen(false);
    };
    window.addEventListener("keydown", closePreview);
    return () => window.removeEventListener("keydown", closePreview);
  }, [mobilePreviewOpen]);
  const executeWithProgress = (code: string) => {
    if (!executeCode(code)) return false;
    useSessionStore.getState().beginGeneration("\u6b63\u5728\u91cd\u65b0\u8ba1\u7b97\u6a21\u578b");
    return true;
  };
  const modifyParametersWithProgress = (
    updates: { parameter_id: string; value: number }[],
  ) => {
    if (!modifyParameters(
      updates,
      model.result?.parameter_state_sha256,
    )) return false;
    useSessionStore.getState().beginGeneration("正在更新 FreeCAD 参数");
    return true;
  };
  const syncDurableChangeSet = useCallback(
    (detail: DurableChangeSetDetail) => {
      const current = useSessionStore.getState();
      if (current.ownerId !== ownerId || current.sessionId !== sessionId) return;
      applyDurableChangeSet(detail, panel.id);
    },
    [applyDurableChangeSet, ownerId, sessionId, panel.id],
  );
  const resumeWithProgress = (runId: string) => {
    if (!resumeRun(runId)) return false;
    useSessionStore.getState().beginGeneration();
    return true;
  };

  const retryTask = async () => {
    if (!task.taskId || task.running) return;
    if (agentBlockedReason) throw new Error(agentBlockedReason);
    const sourcePanel=panel.id;
    const receipt=await retryDurableTask(task.taskId,`${ownerId}:${task.taskId}:retry`);
    const current=useSessionStore.getState();
    if(current.ownerId!==ownerId || current.sessionId!==sessionId) return;
    if(current.activePanelId===sourcePanel) current.beginGeneration("正在恢复原需求并重试");
    current.setDurableWorkflowStarted({...receipt,panel_id:sourcePanel},sourcePanel);
  };
  const latestUserPrompt = panel.messages.filter((message) => message.role === "user").at(-1)?.content || "";
  const restoreSnapshot = (snapshot: ModelSnapshotDetail) => {
    if (useSessionStore.getState().activePanelId !== panel.id) throw new Error("面板已切换，请在当前面板重新选择历史版本");
    if (panel.isGenerating) throw new Error("请先完成或取消当前任务，再恢复历史版本");
    if (snapshot.files?.fcstd || snapshot.result?.files?.fcstd) {
      if (!restoreRevision(snapshot.revision_id || snapshot.id)) return false;
      useSessionStore.getState().beginGeneration("正在准备历史原生版本恢复");
      return true;
    }
    if (!snapshot.code?.trim()) throw new Error("该历史版本没有可恢复的 FCStd 文件或源码");
    return executeWithProgress(snapshot.code);
  };
  const openParameters = () => {
    if (documentView.document?.fcstd) {
      selectInspectorTab("document"); setInspectorCollapsed(false);
      if (window.matchMedia("(max-width: 1180px)").matches) setInspectorOpen(true);
      window.requestAnimationFrame(() => {
        const properties = Array.from(document.querySelectorAll<HTMLElement>('[data-feature-properties]')).find(element => element.offsetParent !== null);
        properties?.scrollIntoView({block:'nearest'});
      });
    } else if (model.parameters.length) setParametersOpen(true);
    else { selectInspectorTab("requirements"); setInspectorCollapsed(false); }
  };
  const recoverTask = (target: string) => {
    setAgentOpen(false);
    if (target === "properties") openParameters();
    else {
      selectInspectorTab("versions"); setInspectorCollapsed(false);
      if (window.matchMedia("(max-width: 1180px)").matches) setInspectorOpen(true);
    }
  };
  const viewKey = `${panel.id}:${documentView.identity?.mode}:${viewIsCommitted ? cloud.document?.document_id : documentView.identity?.viewedRevisionId}`;
  const showCandidate = () => {
    if (panel.result?.success && panel.result.revision_id) documentView.show("candidate", panel.result.revision_id, panel.result.change_set_id);
  };

  if (ownerId !== user.id) {
    return <div className="grid min-h-screen place-items-center type-body text-[var(--muted)]">正在恢复工程会话...</div>;
  }

  if (!hasProject) {
    return (
      <div className="h-[100dvh] min-w-[320px] overflow-hidden bg-white text-[var(--ink)]">
        <div className="ww-app-shell">
          <ProjectSidebar
            activeView="overview"
            collapsed={sidebarCollapsed}
            mobileOpen={mobileSidebar}
            onCollapse={() => setSidebarCollapsed((value) => !value)}
            onLogout={onLogout}
            onMobileClose={() => setMobileSidebar(false)}
            onNavigate={() => setMobileSidebar(false)}
            onNewProject={createProject}
            onSettings={() => setSettingsOpen(true)}
            onUserUpdate={onUserUpdate}
            restoreContext={restoreContext}
            user={user}
          />
          <div className="ww-app-main">
            <header className="workspace-header">
              <button aria-label="打开项目导航" className="workspace-icon-button lg:hidden" onClick={() => setMobileSidebar(true)} type="button"><Icon name="layers" size={17} /></button>
              <span className="workspace-header__title cursor-default">新建设计</span>
              <span aria-label={connectionState === "connected" ? "实时任务连接正常" : undefined} className={`workspace-status hidden md:inline-flex ${connectionState === "connected" ? "text-emerald-700" : "text-amber-700"}`}><span className={connectionState === "connected" ? "bg-emerald-500" : "bg-amber-500"} />{connectionState === "connected" ? null : "实时连接中"}</span>
              <div className="workspace-header__actions">
                <LanguageSwitch className="workspace-button ww-language-switch" />
                <button aria-label="设置" className="workspace-icon-button" onClick={() => setSettingsOpen(true)} type="button"><Icon name="settings" size={16} /></button>
              </div>
            </header>
            <ProjectStart embedded connectionState={connectionState} onOpenHistory={() => setMobileSidebar(true)} onStart={startProject} />
          </div>
        </div>
        <SettingsDrawer onClose={() => setSettingsOpen(false)} open={settingsOpen} />
      </div>
    );
  }

  const versionHistory = (
    <VersionHistoryPanel
      key={`history:${panel.id}`}
      currentRevisionId={cloud.document?.head_revision_id}
      onView={(snapshot) => { if (snapshot.panel_id !== panel.id) return; documentView.show("history", snapshot.revision_id || snapshot.id); }}
      activeSnapshotId={model.result?.snapshot_id}
      currentParts={model.result?.assembly_parts || []}
      onModifyPart={viewIsCommitted ? (name, instruction, partId) => modifyPart(name, instruction, partId, model.result) : undefined}
      onRestore={restoreSnapshot}
      panelId={panel.id}
      refreshKey={model.result?.snapshot_id}
    />
  );
  const documentSections = {
    sharingContent:<CloudDocumentPanel section="sharing" connection={viewedCloud} onSubmitted={onDocumentSubmitted} />,
    branchContent:<CloudDocumentPanel section="versions" connection={viewedCloud} onSubmitted={onDocumentSubmitted} />,
    engineeringContent:<CloudDocumentPanel section="engineering" connection={viewedCloud} onEngineeringTasksChange={onEngineeringTasksChange} />,
    requirementContent:<><div className="ww-inspector-section" data-testid="inspector-task-state" data-task-phase={task.phase}><h3>{task.label}</h3><p className="type-caption">{task.title}</p></div>
      <RequirementSummary objective={task.objective} basis={panel.requirementBasis || (task.snapshot?.request_payload.operation_context as {requirement_basis?:RequirementBasis}|undefined)?.requirement_basis}
        profile={task.snapshot?.request_payload.manufacturing_profile as ManufacturingProfile | undefined} />
      <CloudDocumentPanel section="activity" connection={viewedCloud} onReview={(id)=>{setReviewDocumentChange(id);setChangesOpen(true);}} /></>,
  };
  const inspector = (
    <WorkspaceInspector {...documentSections} activeTab={inspectorTab} onTabChange={selectInspectorTab}
      canExport={canExport} canEdit={canEdit}
      cloudDocument={<CloudDocumentPanel section="features" key={viewKey} connection={viewedCloud} onEngineeringTasksChange={onEngineeringTasksChange} onSubmitted={onDocumentSubmitted} onReview={(id) => { setReviewDocumentChange(id); setChangesOpen(true); }} />}
      artifacts={viewedArtifacts}
      onCollapse={() => { if (inspectorOpen) setInspectorOpen(false); else setInspectorCollapsed(true); }}
      onExport={() => setExportOpen(true)}
      onProperties={openParameters}
      parameters={model.parameters}
      projectId={panel.durable?.projectId}
      revisionId={model.result?.revision_id || panel.durable?.currentRevisionId}
      bom={viewedAgent?.bom}
      project={model.project}
      result={model.result}
      stages={model.stages}
      versionHistory={versionHistory}
      view={view}
    />
  );

  return (
    <div className="h-[100dvh] min-w-[320px] overflow-hidden bg-white text-[var(--ink)]">
      <WorkspaceShell
        agent={<AgentPanel key={agentKey} connection={connectionState} context={view} embedded document={cloud.document} isAdmin={user.is_admin} canModify={canEdit} onRetry={retryTask} onRecover={recoverTask} onReview={()=>setChangesOpen(true)} onCancel={cancelGeneration} onCollapse={() => setAgentCollapsed(true)} onPreview={() => { if (task.phase === "candidate") showCandidate(); else documentView.showCommitted(); setView("mechanical"); setMobilePreviewOpen(true); }} onSend={sendSelectedMessage} suggestedPrompt={agentPrompt} selectionLabel={selectionLabel} onClearSelection={cloud.clearSelection} blockedReason={agentBlockedReason} />}
        agentCollapsed={agentCollapsed}
        header={<WorkspaceHeader documentSynced={cloud.connected} agentVisible={!agentCollapsed} viewMode={documentView.identity?.mode} canExport={canExport} connection={connectionState} onAgent={() => askAgent()} onBack={() => navigate("overview")} onChanges={() => setChangesOpen(true)} onChecks={() => setChecksOpen(true)} onExport={() => setExportOpen(true)} onMenu={() => setMobileSidebar(true)} onPreview={() => setMobilePreviewOpen((value) => !value)} onSettings={() => setSettingsOpen(true)} previewOpen={mobilePreviewOpen} project={model.project} />}
        inspector={inspector}
        inspectorCollapsed={inspectorCollapsed}
        inspectorOverlayOpen={inspectorOpen}
        onInspectorOverlayClose={() => setInspectorOpen(false)}
        mobilePreviewOpen={mobilePreviewOpen}
        onAgentCollapse={() => setAgentCollapsed(false)}
        onInspectorCollapse={() => setInspectorCollapsed(false)}
        sidebar={<ProjectSidebar activeView={view} collapsed={sidebarCollapsed} mobileOpen={mobileSidebar} onCollapse={() => setSidebarCollapsed((value) => !value)} onLogout={() => guardDraft(onLogout)} onMobileClose={() => setMobileSidebar(false)} onNavigate={navigate} onNewProject={createProject} onSettings={() => setSettingsOpen(true)} onUserUpdate={onUserUpdate} restoreContext={restoreContext} user={user} />}
      >
        {view === "overview" ? <ProjectFlow onOpenStage={openStage} project={model.project} stages={model.stages} task={model.task} /> : null}
        {view === "simulation" ? <section className="h-full overflow-y-auto p-6" aria-label="结构仿真工作台">
          <h1 className="type-section-heading mb-4">结构仿真</h1>
          {documentView.document?.fcstd ? <EngineeringTasks key={`simulation:${viewKey}`} document={documentView.document} initiallyOpen onTasksChange={onEngineeringTasksChange} />
            : <p role="status">提交原生 CAD 模型后可配置材料、载荷与边界条件。</p>}
        </section> : null}
        {view === "mechanical" ? <MechanicalWorkspace nativeDocumentId={documentView.document?.fcstd ? documentView.document.document_id : undefined} viewedMeshUrl={documentView.document?.mesh?.url}
          identity={documentView.identity} selectedId={viewedSelectedId} onSelect={selectViewedFeature} onCommitted={documentView.showCommitted}
          onCandidate={panel.result?.success && panel.result.revision_id && panel.result.revision_id !== documentView.identity?.viewedRevisionId && panel.result.revision_id !== cloud.document?.head_revision_id ? showCandidate : undefined}
          viewError={documentView.error} taskLabel={task.label} taskPhase={task.phase} hasParameters={Boolean(model.parameters.length || documentView.document?.fcstd)} currentStep={panel.currentStep} isGenerating={task.running} onAgent={() => askAgent()} onBack={() => navigate("overview")} onInspector={openParameters} onProperties={openParameters} result={model.result} /> : null}
      </WorkspaceShell>

      {pendingRequest && (!panel.isGenerating || connectionState !== "connected") ? <div role="status" className="fixed bottom-16 left-1/2 z-40 flex w-[min(90%,480px)] -translate-x-1/2 flex-wrap items-center gap-2 rounded border border-amber-300 bg-amber-50 p-3 type-caption">
        原请求尚待确认 · {pendingRequest.idempotency_key.slice(0,8)}
        <button className="workspace-button" type="button" disabled={connectionState !== "connected"} onClick={retryPendingSubmission}>查询原请求状态</button>
      </div> : null}

      <ParameterDrawer isGenerating={panel.isGenerating || !viewIsCommitted} key={`parameters:${model.result?.request_id || "empty"}:${parametersOpen}`} onClose={() => guardDraft(() => setParametersOpen(false))} onExecute={executeWithProgress} onModifyParameters={modifyParametersWithProgress} open={parametersOpen} parameters={model.parameters} result={model.result} />
      <AgentDrawer connection={connectionState} context={view} document={cloud.document} isAdmin={user.is_admin} canModify={canEdit} onRetry={retryTask} onRecover={recoverTask} onReview={()=>setChangesOpen(true)} key={`${agentKey}:${view}:${agentOpen}`} onCancel={cancelGeneration} onClose={() => { setAgentOpen(false); setAgentSuggestion(null); }} onSend={sendSelectedMessage} open={agentOpen} suggestedPrompt={agentPrompt}
        selectionLabel={selectionLabel} onClearSelection={cloud.clearSelection} blockedReason={agentBlockedReason} />
      <ValidationDialog
        viewLabel={viewLabel(documentView.identity)}
        activeSnapshotId={model.result?.snapshot_id}
        activeRun={currentResultVisible ? panel.activeRun : null}
        analysis={activeAnalysis}
        artifacts={viewedArtifacts}
        durableAgent={viewedAgent}
        description={latestUserPrompt}
        isGenerating={panel.isGenerating}
        key={"validation:" + (documentView.identity?.viewedRevisionId || model.result?.request_id || "empty")}
        onAnalysis={(analysis) => {
          const requestId = model.result?.request_id;
          if (!requestId) return;
          setAnalysisByRequest((evidence) => ({
            ...evidence,
            [requestId]: analysis,
          }));
        }}
        onAskAgent={(prompt) => { setChecksOpen(false); askAgent(prompt); }}
        onClose={() => setChecksOpen(false)}
        onModifyPart={viewIsCommitted ? (name, instruction, partId) => modifyPart(name, instruction, partId, model.result) : undefined}
        onRerunCode={viewIsCommitted ? () => model.result?.code ? executeWithProgress(model.result.code) : undefined : undefined}
        onRestore={restoreSnapshot}
        onResumeRun={viewIsCommitted ? resumeWithProgress : undefined}
        onRetryPrompt={viewIsCommitted ? () => latestUserPrompt ? startProject(latestUserPrompt, model.result?.manufacturing_profile || null) : false : undefined}
        open={checksOpen}
        panelId={panel.id}
        refreshKey={model.result?.snapshot_id}
        result={model.result}
        steps={currentResultVisible ? panel.stepHistory : []}
      />
      <ChangeSetDialog
        key={`${panel.id}:${reviewDocumentChange || panel.durable?.changeSetId || "legacy"}:${changesOpen}`}
        activeSnapshotId={model.result?.snapshot_id}
        changeSetId={reviewDocumentChange || panel.durable?.changeSetId}
        onAskAgent={(prompt) => { setChangesOpen(false); askAgent(prompt); }}
        onClose={() => { setChangesOpen(false); setReviewDocumentChange(null); }}
        onDurableChangeSet={syncDurableChangeSet}
        onApplied={async () => { await cloud.refresh?.(); documentView.showCommitted(); }}
        onRestore={restoreSnapshot}
        open={changesOpen}
        panelId={panel.id}
      />
      <ExportDialog canExport={canExport} key={`export:${panel.id}:${documentView.identity?.viewedRevisionId}`} viewLabel={viewLabel(documentView.identity)} jobs={model.exports} onClose={() => setExportOpen(false)} open={exportOpen} requestId={model.result?.request_id} />
      <SettingsDrawer onClose={() => setSettingsOpen(false)} open={settingsOpen} />
    </div>
  );
}
