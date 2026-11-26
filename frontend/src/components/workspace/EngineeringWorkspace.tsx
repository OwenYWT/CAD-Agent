import { RequirementSummary } from "../agent/RequirementCard";
import type { InspectorTab } from "./WorkspaceInspector";
import type { RequirementBasis } from "../../types/requirements";
import { taskState } from "../../adapters/taskState";
import { retryDurableTask } from "../../services/clients/tasks";
import { createPortal } from "react-dom";
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
import { getModelSnapshot, listModelSnapshots } from "../../services/clients/revisions";
import { listEngineeringTasks } from "../../services/clients/engineering";
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
import { useI18n } from "../../i18n/I18nContext";
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
  const { translate } = useI18n();
  const ownerId = useSessionStore((state) => state.ownerId);
  const bindOwner = useSessionStore((state) => state.bindOwner);
  const sessionId = useSessionStore((state) => state.sessionId);
  const panel = useSessionStore((state) => state.getActivePanel());
  const cloud = useCloudDocument(panel.durable?.branchId || null);
  const task = useMemo(() => taskState(panel,cloud.document), [panel, cloud.document]);
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
  const [selectionSource, setSelectionSource] = useState<{id:string; documentId:string; source:"viewport"|"tree"} | null>(null);
  const selectViewedFeature = (id: string | null) => {
    if (id) setSelectionSource({id,documentId:documentView.identity?.documentId || "",source:"tree"});
    if (viewIsCommitted) cloud.select(id);
    else if (documentView.identity) guardDraft(() => setViewSelection({ revision: documentView.identity!.viewedRevisionId, id }));
  };
  const viewedCloud = { ...cloud, document: documentView.document, selectedId: viewedSelectedId, select: selectViewedFeature,
    error: documentView.error || cloud.error };
  const currentResultVisible = !documentView.identity || hydratedPanel.result?.revision_id === documentView.identity.viewedRevisionId;
  const viewedArtifacts = currentResultVisible ? panel.artifactUpdates : [];
  const viewedAgent = currentResultVisible ? panel.durable?.agent : null;
  const draftCount = useDraftGuardStore(state => Object.keys(state.drafts).length);
  const [continuation, setContinuation] = useState<{ panelId: string; candidateRevisionId: string; baseRevisionId: string } | null>(null);
  const continuationSource = continuation?.panelId === panel.id ? continuation : null;
  const agentBlockedReason = continuationSource && continuationSource.baseRevisionId !== cloud.document?.head_revision_id ? '候选的原始保存基线已变化，请取消续改并重新核对范围。'
    : task.phase === "candidate" && documentView.identity?.mode !== "history" ? "在候选审阅中可以保存版本或提出修改。"
    : !viewIsCommitted ? "请先返回已提交版本，再请求 AI 继续修改。"
    : pendingRequest ? "正在确认原请求是否受理，请先查询原请求状态。"
    : draftCount ? "请先提交或放弃手动编辑草稿，再让 AI 修改。"
      : panel.durable?.branchId && !cloud.connected ? "正在同步已提交文档，连接恢复后可继续修改。" : undefined;
  const selectedFeature = cloud.selectionContext ? cloud.document?.features.find(f => f.id === cloud.selectionContext?.feature_ids[0]) : null;
  const selectedFromViewport = selectionSource?.id === viewedSelectedId && selectionSource?.documentId === documentView.identity?.documentId && selectionSource?.source === "viewport";
  const selectionKind = cloud.selectionContext?.topology_selector ? `${translate(cloud.selectionContext.topology_selector.subelement_kind === "face" ? "面" : "圆边")} ${cloud.selectionContext.topology_selector.axis}/${cloud.selectionContext.topology_selector.extreme}` : translate(selectedFromViewport ? "整个部件（显示特征）" : selectedFeature?.type === "PartDesign::Hole" ? "孔特征" : "特征");
  const selectionLabel = continuationSource ? `${translate('续改候选')} ${continuationSource.candidateRevisionId.slice(0,8)} · ${translate('原保存基线')} ${continuationSource.baseRevisionId.slice(0,8)}` : selectedFeature && cloud.selectionContext ? `${selectionKind}：${selectedFeature.label} · ${selectedFeature.type} · ${translate('修订')} ${cloud.selectionContext.revision_id.slice(0, 8)}` : undefined;
  const sendSelectedMessage = (text: string, basis?: RequirementBasis) => {
    if (agentBlockedReason) return false;
    if (continuationSource && continuationSource.baseRevisionId !== cloud.document?.head_revision_id) return false;
    const sent = sendMessage(text, "auto", null, continuationSource ? undefined : cloud.selectionContext, basis, continuationSource?.candidateRevisionId);
    if (sent) setContinuation(null);
    return sent;
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
  const [drawerSelection, setDrawerSelection] = useState<{panelId:string;kind:"properties"|"checks"|"versions"|"export"|"tools"}|null>(null);
  const drawer = drawerSelection?.panelId === panel.id ? drawerSelection.kind : null;
  const openDrawer = (kind: NonNullable<typeof drawer>) => { setDrawerSelection({panelId:panel.id,kind}); };
  const closeDrawer = () => { setDrawerSelection(null); setAgentCollapsed(false); };
  const inspectorCollapsed = drawer === null;
  const [inspectorSelection,setInspectorSelection]=useState<{panelId:string;tab:InspectorTab} | null>(null);
  const inspectorTab=inspectorSelection?.panelId===panel.id ? inspectorSelection.tab : documentView.document?.fcstd ? "document" : "requirements";
  const selectInspectorTab=(tab:InspectorTab)=> {
    if (tab !== inspectorTab) guardDraft(() => setInspectorSelection({panelId:panel.id,tab}));
  };

  const [mobileSidebar, setMobileSidebar] = useState(false);
  const openSidebar = () => { setSidebarCollapsed(false); setMobileSidebar(true); };
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [expandedPanel, setExpandedPanel] = useState<string | null>(null);
  // Let the same renderer, camera and LOD move between the chat and full workspace.
  const [modelHost] = useState(() => {
    const host = document.createElement("div");
    host.className = "ww-model-host";
    return host;
  });
  const attachModel = useCallback((slot: HTMLDivElement | null) => {
    if (slot) slot.appendChild(modelHost);
  }, [modelHost]);
  const modelOpen = expandedPanel === panel.id;
  const hasPreview = Boolean(documentView.document?.fcstd || documentView.document?.mesh?.url
    || viewedResult?.files?.stl || viewedResult?.files?.svg);
  const conversationOnly = view === "mechanical" && !drawer && !modelOpen;
  const openModel = () => {
    setView("mechanical");
    setDrawerSelection(null);
    setExpandedPanel(panel.id);
  };
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
  const parametersOpen = drawer === "properties" && !documentView.document?.fcstd;
  const checksOpen = drawer === "checks";
  const setChecksOpen = (open:boolean) => open ? openDrawer("checks") : closeDrawer();
  const [changesOpen, setChangesOpen] = useState(false);
  const [reviewTarget, setReviewTarget] = useState<{panelId: string; changeId: string; note?: string} | null>(null);
  const selectedReview = reviewTarget?.panelId === panel.id ? reviewTarget : null;
  const reviewChangeId = selectedReview?.changeId || panel.durable?.changeSetId;
  const openReview = (changeId?: string | null, note?: string) => {
    setReviewTarget(changeId ? {panelId: panel.id, changeId, note} : null);
    setChangesOpen(true);
  };
  const closeReview = () => { setChangesOpen(false); setReviewTarget(null); };
  const canDraftReview = task.phase === "candidate" && task.changeStatus === "pending_review" && !!task.changeSetId
    && (!panel.durable?.branchId || (documentView.identity?.mode === "candidate"
      && documentView.identity.viewedRevisionId === (task.snapshot?.change_set?.candidate_revision_id || panel.result?.revision_id)))
    && (cloud.document?.can_review ?? canEdit) && !draftCount && !pendingRequest;
  const exportOpen = drawer === "export";
  const setExportOpen = (open:boolean) => open ? openDrawer("export") : closeDrawer();

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
    setView("mechanical"); setAgentCollapsed(false); setExpandedPanel(null);
    return true;
  };

  const navigate = (next: string) => guardDraft(() => {
    setView(next as EngineeringDomain);
    setExpandedPanel(null);
    setDrawerSelection(null);
    setAgentCollapsed(false);
  });
  const createProject = () => {
    guardDraft(() => { addPanel(); setView("mechanical"); });
  };
  const openStage = (stage: EngineeringStage) => {
    if (!stage.available) return;
    if (stage.id === "requirements" && model.result?.design_brief) { setChecksOpen(true); return; }
    if (stage.id === "manufacturing") { setChecksOpen(true); return; }
    if (stage.id === "release") { setExportOpen(true); return; }
    if (stage.domain === "mechanical") openModel();
    else if (stage.domain) setView(stage.domain);
  };
  const askAgent = (prompt = "") => {
    closeDrawer();
    if (prompt) {
      setAgentSuggestion((previous) => ({
        contextKey: agentContextKey,
        prompt,
        sequence: (previous?.sequence || 0) + 1,
      }));
    }
    setAgentCollapsed(false);
    setView("mechanical");
    setExpandedPanel(null);
  };

  useEffect(() => {
    if (!modelOpen || drawer) return;
    const closePreview = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented && !document.querySelector('[aria-modal="true"], [data-menu-open="true"]')) setExpandedPanel(null);
    };
    window.addEventListener("keydown", closePreview);
    return () => window.removeEventListener("keydown", closePreview);
  }, [modelOpen, drawer]);
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
  const openParameters = () => { openDrawer("properties"); };
  const recoverTask = (target: string,featureId?:string) => {
    if (target === "properties") {documentView.showCommitted();if(featureId) cloud.select(featureId);openParameters();}
    else openDrawer("versions");
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
            onNavigate={navigate}
            collapsed={sidebarCollapsed}
            mobileOpen={mobileSidebar}
            onCollapse={() => setSidebarCollapsed((value) => !value)}
            onLogout={onLogout}
            onMobileClose={() => setMobileSidebar(false)}
            onNewProject={createProject}
            onSettings={() => setSettingsOpen(true)}
            onUserUpdate={onUserUpdate}
            restoreContext={restoreContext}
            user={user}
          />
          <div className="ww-app-main">
            <header className="workspace-header ww-chat-header">
              <button aria-label="打开项目导航" className="workspace-icon-button xl:hidden" onClick={openSidebar} type="button"><Icon name="sidebar" size={17} /></button>
              <span className="workspace-header__title cursor-default">CAD-Agent</span>
              {connectionState === "connected" ? <span role="status" aria-label="实时任务连接正常" title="实时任务连接正常" className="ww-connection-dot" /> : null}
              <div className="workspace-header__actions">
                <button aria-label="设置" title="设置" className="workspace-icon-button" onClick={() => setSettingsOpen(true)} type="button"><Icon name="settings" size={16} /></button>
                <LanguageSwitch className="workspace-button ww-language-switch" />
              </div>
            </header>
            <ProjectStart key={panel.id} embedded connectionState={connectionState} onOpenHistory={openSidebar} onStart={startProject} importContext={{sessionId,panelId:panel.id,onImported:receipt=>{
              const current=useSessionStore.getState();
              if (current.ownerId!==ownerId || current.sessionId!==sessionId) return;
              current.setDurableWorkflowStarted(receipt,panel.id);setView('mechanical');setAgentCollapsed(false);
            }}} />
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
      <RequirementSummary objective={documentView.evidence?.objective || documentView.evidence?.snapshot?.prompt || task.objective}
        basis={documentView.evidence ? documentView.evidence.requirement_basis : panel.requirementBasis || (task.snapshot?.request_payload.operation_context as {requirement_basis?:RequirementBasis}|undefined)?.requirement_basis}
        profile={documentView.evidence?.manufacturing_profile || model.result?.manufacturing_profile || (!documentView.evidence ? task.snapshot?.request_payload.manufacturing_profile as ManufacturingProfile | undefined : undefined)} />
      <CloudDocumentPanel section="activity" connection={viewedCloud} onReview={openReview} /></>,
  };
  const inspector = (
    <WorkspaceInspector {...documentSections} showHeader={false} showTabs={drawer === "tools"} visible={drawer === "tools" || drawer === "versions" || drawer === "properties"} activeTab={drawer === "properties" ? "document" : drawer === "versions" ? "versions" : inspectorTab} onTabChange={selectInspectorTab}
      canExport={canExport} canEdit={canEdit}
      cloudDocument={<CloudDocumentPanel durable={panel.durable} section="features" key={viewKey} connection={viewedCloud} onEngineeringTasksChange={onEngineeringTasksChange} onSubmitted={onDocumentSubmitted} onReview={openReview} />}
      artifacts={viewedArtifacts}
      onCollapse={closeDrawer}
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
      view="mechanical"
    />
  );

  const drawerContent = <div className="ww-drawer-content">
    <h2 className="ww-drawer-title">{drawer ? ({properties:"属性",checks:"检查",versions:"版本",export:"导出",tools:"更多工具"})[drawer] : ""}</h2>
    <div className="ww-drawer-task" data-testid="drawer-task-state" data-task-phase={task.phase}><strong>{task.label}</strong><span>{task.title}</span>{draftCount > 0 ? <small>我的未提交修改 · {draftCount} 处草稿</small> : null}</div>
    <div className="ww-drawer-host" hidden={!(drawer === "tools" || drawer === "versions" || drawer === "properties" && !parametersOpen)}>{inspector}</div>
      <ParameterDrawer embedded canEdit={canEdit && viewIsCommitted} isGenerating={task.running} key={`parameters:${model.result?.request_id || "empty"}`} onClose={closeDrawer} onExecute={executeWithProgress} onModifyParameters={modifyParametersWithProgress} open={parametersOpen} parameters={model.parameters} result={model.result} />
      <ValidationDialog embedded
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

      <ExportDialog embedded canExport={canExport} key={`export:${panel.id}:${documentView.identity?.viewedRevisionId}`} viewLabel={viewLabel(documentView.identity)} jobs={model.exports} onClose={() => setExportOpen(false)} open={exportOpen} requestId={model.result?.request_id} />
  </div>;

  const mechanical = <MechanicalWorkspace onExpand={conversationOnly && hasPreview ? openModel : undefined} savedDocument={cloud.document} onSelectTopology={viewIsCommitted ? cloud.selectTopology : undefined} nativeDocument={documentView.document} nativeDocumentId={documentView.document?.fcstd ? documentView.document.document_id : documentView.loading && cloud.document?.fcstd ? cloud.document.document_id : undefined} viewedMeshUrl={documentView.document?.mesh?.url}
          identity={documentView.identity} selectedId={viewedSelectedId} selectionLabel={viewedSelectedId ? `${translate(selectedFromViewport ? '整个部件（显示特征）' : documentView.document?.features.find(f=>f.id===viewedSelectedId)?.type==='PartDesign::Hole' ? '孔特征' : '特征')}：${documentView.document?.features.find(f=>f.id===viewedSelectedId)?.label || ''}` : undefined}
          onSelect={id=>{selectViewedFeature(id);setSelectionSource({id,documentId:documentView.identity?.documentId || "",source:"viewport"});}} onOpenProperties={openParameters} onCommitted={documentView.showCommitted}
          onCandidate={panel.result?.success && panel.result.revision_id && panel.result.revision_id !== documentView.identity?.viewedRevisionId && panel.result.revision_id !== cloud.document?.head_revision_id ? showCandidate : undefined}
          viewError={documentView.error} taskLabel={task.label} taskPhase={task.phase} hasParameters={Boolean(model.parameters.length || documentView.document?.fcstd)} currentStep={panel.currentStep} isGenerating={task.running} onAgent={() => askAgent()} onBack={() => navigate("overview")} onInspector={openParameters} onProperties={openParameters} result={model.result} />;

  return (
    <div className="h-[100dvh] min-w-[320px] overflow-hidden bg-white text-[var(--ink)]">
      <WorkspaceShell
        conversationOnly={conversationOnly}
        agent={<AgentPanel key={agentKey} mainThread={conversationOnly} onReviewDraft={canDraftReview ? note => openReview(task.changeSetId, note) : undefined} modelArtifact={conversationOnly && hasPreview ? <div ref={attachModel} className="ww-model-slot" /> : null} viewError={conversationOnly && !hasPreview ? documentView.error : undefined} connection={connectionState} context={view} embedded document={cloud.document} isAdmin={user.is_admin} canModify={canEdit} canConfirm={cloud.document?.can_review ?? canEdit} onRetry={retryTask} onRecover={recoverTask} onReview={() => openReview(task.changeSetId)} onCancel={cancelGeneration} onCollapse={() => setAgentCollapsed(true)} onPreview={() => { if (task.phase === "candidate") showCandidate(); else documentView.showCommitted(); openModel(); }} onSend={sendSelectedMessage} suggestedPrompt={agentPrompt} selectionLabel={selectionLabel} onClearSelection={()=>{cloud.clearSelection?.();setContinuation(null);}} blockedReason={agentBlockedReason} />}
        agentCollapsed={agentCollapsed && !conversationOnly}
        header={<><WorkspaceHeader onMechanical={() => guardDraft(openModel)} compact={conversationOnly} canPreview={(!conversationOnly && hasPreview) || Boolean(documentView.error) || view !== "mechanical"} activeTool={drawer} documentSynced={cloud.connected} documentSyncStatus={cloud.syncStatus} documentSyncError={cloud.error} viewMode={documentView.identity?.mode} canExport={canExport} connection={connectionState} onBack={() => navigate("overview")} onProperties={openParameters} onVersions={() => openDrawer("versions")} onTools={() => openDrawer("tools")} onChecks={() => setChecksOpen(true)} onExport={() => setExportOpen(true)} onMenu={openSidebar} onPreview={() => modelOpen || view !== "mechanical" ? askAgent() : openModel()} onSettings={() => setSettingsOpen(true)} previewOpen={modelOpen || view !== "mechanical"} project={model.project} />
          {!conversationOnly && (modelOpen || agentCollapsed) && !drawer && ["failed","needs_input","candidate"].includes(task.phase) ? <button type="button" className="ww-mobile-task-alert workspace-button"
            onClick={() => task.phase === "candidate" ? openReview(task.changeSetId) : askAgent()}>{task.phase === "candidate" ? "审阅候选与参数变化" : `${task.title} · 点击处理`}</button> : null}</>}
        inspector={drawerContent}
        inspectorCollapsed={inspectorCollapsed}
        inspectorOverlayOpen={drawer !== null}
        onInspectorOverlayClose={closeDrawer}
        mobilePreviewOpen={modelOpen || view !== "mechanical"}
        onAgentCollapse={() => setAgentCollapsed(false)}
        onInspectorCollapse={closeDrawer}
        sidebar={<ProjectSidebar collapsed={sidebarCollapsed} mobileOpen={mobileSidebar} onCollapse={() => setSidebarCollapsed((value) => !value)} onLogout={() => guardDraft(onLogout)} onMobileClose={() => setMobileSidebar(false)} onNavigate={navigate} onNewProject={createProject} onSettings={() => setSettingsOpen(true)} onUserUpdate={onUserUpdate} restoreContext={restoreContext} user={user} />}
      >
        {view === "overview" ? <ProjectFlow onOpenStage={openStage} project={model.project} stages={model.stages} task={model.task} /> : null}
        {view === "simulation" ? <section className="h-full overflow-y-auto p-6" aria-label="结构仿真工作台">
          <h1 className="type-section-heading mb-4">结构仿真</h1>
          {documentView.document?.fcstd ? <EngineeringTasks key={`simulation:${viewKey}`} document={documentView.document} initiallyOpen onTasksChange={onEngineeringTasksChange} />
            : <p role="status">提交原生 CAD 模型后可配置材料、载荷与边界条件。</p>}
        </section> : null}
        {view === "mechanical" && (!conversationOnly || !hasPreview) ? <div ref={attachModel} className="ww-model-slot" /> : null}
      </WorkspaceShell>
      {view === "mechanical" ? createPortal(mechanical, modelHost, panel.id) : null}

      {pendingRequest && (!panel.isGenerating || connectionState !== "connected") ? <div role="status" className="fixed bottom-16 left-1/2 z-40 flex w-[min(90%,480px)] -translate-x-1/2 flex-wrap items-center gap-2 rounded border border-amber-300 bg-amber-50 p-3 type-caption">
        原请求尚待确认 · {pendingRequest.idempotency_key.slice(0,8)}
        <button className="workspace-button" type="button" disabled={connectionState !== "connected"} onClick={retryPendingSubmission}>查询原请求状态</button>
      </div> : null}

      <ChangeSetDialog
        key={`${panel.id}:${reviewChangeId || "legacy"}:${changesOpen}`}
        activeSnapshotId={model.result?.snapshot_id}
        changeSetId={reviewChangeId}
        initialReviewNote={selectedReview?.note}
        onAskAgent={(prompt, source) => { setContinuation(source ? { ...source, panelId: panel.id } : null); closeReview(); documentView.showCommitted(); askAgent(prompt); }}
        onClose={closeReview}
        onDurableChangeSet={syncDurableChangeSet}
        onReviewed={async (status) => {
          await cloud.refresh?.();
          // A rejected/requested-change candidate restores its input draft. A
          // background refresh must not act like the user leaving that draft.
          if (status === "committed" || status === "rolled_back") documentView.showCommitted();
        }}
        onRestore={restoreSnapshot}
        open={changesOpen}
        panelId={panel.id}
      />

      <SettingsDrawer onClose={() => setSettingsOpen(false)} open={settingsOpen} />
    </div>
  );
}
