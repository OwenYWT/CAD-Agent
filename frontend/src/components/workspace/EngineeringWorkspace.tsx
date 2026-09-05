import { useCallback, useEffect, useMemo, useState } from "react";
import type { AuthUser } from "../../auth";
import { adaptEngineeringProject } from "../../adapters/projectAdapter";
import {
  hydrateGenerationResult,
  resultMatchesSnapshot,
} from "../../adapters/resultSnapshotAdapter";
import { useWebSocket } from "../../hooks/useWebSocket";
import {
  getModelSnapshot,
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
import { WorkspaceDrawer } from "../common/WorkspaceOverlay";
import { LanguageSwitch } from "../../i18n/LanguageSwitch";
import WorkspaceShell from "./WorkspaceShell";
import WorkspaceInspector from "./WorkspaceInspector";

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
  const applyDurableChangeSet = useSessionStore(
    (state) => state.applyDurableChangeSet,
  );
  const addPanel = useSessionStore((state) => state.addPanel);
  const { connectionState, sendMessage, executeCode, restoreRevision, resumeRun, restoreContext, modifyPart, modifyParameters, cancelGeneration } = useWebSocket();
  const [snapshotEvidence, setSnapshotEvidence] = useState<ModelSnapshotDetail | null>(null);
  const [analysisByRequest, setAnalysisByRequest] = useState<Record<string, DesignAnalysis>>({});
  const hydratedPanel = useMemo(() => {
    if (!panel.result || !snapshotEvidence) return panel;
    const result = hydrateGenerationResult(panel.result, snapshotEvidence);
    return result === panel.result ? panel : { ...panel, result };
  }, [panel, snapshotEvidence]);
  const activeAnalysis = hydratedPanel.result?.request_id
    ? analysisByRequest[hydratedPanel.result.request_id] || null
    : null;
  const model = useMemo(
    () => adaptEngineeringProject(sessionId, hydratedPanel, activeAnalysis),
    [activeAnalysis, hydratedPanel, sessionId],
  );
  const hasProject = panel.messages.length > 0 || panel.result !== null || panel.isGenerating;
  const [view, setView] = useState<EngineeringDomain>("mechanical");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [agentCollapsed, setAgentCollapsed] = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
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

  const startProject = (prompt: string, profile: ManufacturingProfile | null = null) => {
    if (!sendMessage(prompt, "auto", profile)) return false;
    useSessionStore.getState().beginGeneration();
    useSessionStore.getState().addMessage({ role: "user", content: prompt });
    setView("mechanical");
    return true;
  };

  const navigate = (next: string) => setView(next as EngineeringDomain);
  const createProject = () => {
    addPanel();
    setView("mechanical");
  };
  const openStage = (stage: EngineeringStage) => {
    if (!stage.available) return;
    if (stage.id === "requirements" && model.result?.design_brief) { setChecksOpen(true); return; }
    if (stage.id === "manufacturing") { setChecksOpen(true); return; }
    if (stage.id === "release") { setExportOpen(true); return; }
    if (stage.domain) setView(stage.domain);
  };
  const askAgent = (prompt = "") => {
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
    if (prompt || window.matchMedia("(max-width: 899px)").matches) setAgentOpen(true);
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
      applyDurableChangeSet(detail, panel.id);
    },
    [applyDurableChangeSet, panel.id],
  );
  const resumeWithProgress = (runId: string) => {
    if (!resumeRun(runId)) return false;
    useSessionStore.getState().beginGeneration();
    return true;
  };

  const latestUserPrompt = panel.messages.filter((message) => message.role === "user").at(-1)?.content || "";
  const restoreSnapshot = (snapshot: ModelSnapshotDetail) => {
    if (useSessionStore.getState().activePanelId !== panel.id) throw new Error("面板已切换，请在当前面板重新选择历史版本");
    if (panel.isGenerating) throw new Error("请先完成或取消当前任务，再恢复历史版本");
    if (snapshot.files?.fcstd || snapshot.result?.files?.fcstd) {
      if (!restoreRevision(snapshot.id)) return false;
      useSessionStore.getState().beginGeneration("正在准备历史原生版本恢复");
      return true;
    }
    if (!snapshot.code?.trim()) throw new Error("该历史版本没有可恢复的 FCStd 文件或源码");
    return executeWithProgress(snapshot.code);
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
      activeSnapshotId={model.result?.snapshot_id}
      currentParts={model.result?.assembly_parts || []}
      onModifyPart={modifyPart}
      onRestore={restoreSnapshot}
      panelId={panel.id}
      refreshKey={model.result?.snapshot_id}
    />
  );
  const inspector = (
    <WorkspaceInspector
      artifacts={panel.artifactUpdates}
      onCollapse={() => setInspectorCollapsed(true)}
      onExport={() => setExportOpen(true)}
      onProperties={() => setParametersOpen(true)}
      parameters={model.parameters}
      projectId={panel.durable?.projectId}
      revisionId={model.result?.revision_id || panel.durable?.currentRevisionId}
      bom={panel.durable?.agent?.bom}
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
        agent={<AgentPanel key={agentKey} connection={connectionState} context={view} embedded onCancel={cancelGeneration} onCollapse={() => setAgentCollapsed(true)} onPreview={() => { setView("mechanical"); setMobilePreviewOpen(true); }} onSend={sendMessage} suggestedPrompt={agentPrompt} />}
        agentCollapsed={agentCollapsed}
        header={<WorkspaceHeader connection={connectionState} onAgent={() => askAgent()} onBack={() => setView("overview")} onChanges={() => setChangesOpen(true)} onChecks={() => setChecksOpen(true)} onExport={() => setExportOpen(true)} onMenu={() => setMobileSidebar(true)} onPreview={() => setMobilePreviewOpen((value) => !value)} onSettings={() => setSettingsOpen(true)} previewOpen={mobilePreviewOpen} project={model.project} />}
        inspector={inspector}
        inspectorCollapsed={inspectorCollapsed}
        mobilePreviewOpen={mobilePreviewOpen}
        onAgentCollapse={() => setAgentCollapsed(false)}
        onInspectorCollapse={() => setInspectorCollapsed(false)}
        sidebar={<ProjectSidebar activeView={view} collapsed={sidebarCollapsed} mobileOpen={mobileSidebar} onCollapse={() => setSidebarCollapsed((value) => !value)} onLogout={onLogout} onMobileClose={() => setMobileSidebar(false)} onNavigate={navigate} onNewProject={createProject} onSettings={() => setSettingsOpen(true)} onUserUpdate={onUserUpdate} restoreContext={restoreContext} user={user} />}
      >
        {view === "overview" ? <ProjectFlow onOpenStage={openStage} project={model.project} stages={model.stages} task={model.task} /> : null}
        {view === "mechanical" ? <MechanicalWorkspace currentStep={panel.currentStep} isGenerating={panel.isGenerating} onAgent={() => askAgent()} onBack={() => setView("overview")} onInspector={() => setInspectorOpen(true)} onProperties={() => setParametersOpen(true)} result={model.result} /> : null}
      </WorkspaceShell>

      <ParameterDrawer isGenerating={panel.isGenerating} key={`parameters:${model.result?.request_id || "empty"}:${parametersOpen}`} onClose={() => setParametersOpen(false)} onExecute={executeWithProgress} onModifyParameters={modifyParametersWithProgress} open={parametersOpen} parameters={model.parameters} result={model.result} />
      <AgentDrawer connection={connectionState} context={view} key={`${agentKey}:${view}:${agentOpen}`} onCancel={cancelGeneration} onClose={() => { setAgentOpen(false); setAgentSuggestion(null); }} onSend={sendMessage} open={agentOpen} suggestedPrompt={agentPrompt} />
      <WorkspaceDrawer description="显示当前模型的真实参数、版本、代码和文件。" onClose={() => setInspectorOpen(false)} open={inspectorOpen} title="机械设计检查器"><WorkspaceInspector artifacts={panel.artifactUpdates} bom={panel.durable?.agent?.bom} onExport={() => { setInspectorOpen(false); setExportOpen(true); }} onProperties={() => { setInspectorOpen(false); setParametersOpen(true); }} parameters={model.parameters} project={model.project} projectId={panel.durable?.projectId} result={model.result} revisionId={model.result?.revision_id || panel.durable?.currentRevisionId} showHeader={false} stages={model.stages} versionHistory={versionHistory} view={view} /></WorkspaceDrawer>
      <ValidationDialog
        activeSnapshotId={model.result?.snapshot_id}
        activeRun={panel.activeRun}
        analysis={activeAnalysis}
        artifacts={panel.artifactUpdates}
        durableAgent={panel.durable?.agent}
        description={latestUserPrompt}
        isGenerating={panel.isGenerating}
        key={"validation:" + (model.result?.request_id || "empty")}
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
        onModifyPart={modifyPart}
        onRerunCode={() => model.result?.code ? executeWithProgress(model.result.code) : undefined}
        onRestore={restoreSnapshot}
        onResumeRun={resumeWithProgress}
        onRetryPrompt={() => latestUserPrompt ? startProject(latestUserPrompt, model.result?.manufacturing_profile || null) : false}
        open={checksOpen}
        panelId={panel.id}
        refreshKey={model.result?.snapshot_id}
        result={model.result}
        steps={panel.stepHistory}
      />
      <ChangeSetDialog
        activeSnapshotId={model.result?.snapshot_id}
        changeSetId={panel.durable?.changeSetId}
        onAskAgent={(prompt) => { setChangesOpen(false); askAgent(prompt); }}
        onClose={() => setChangesOpen(false)}
        onDurableChangeSet={syncDurableChangeSet}
        onRestore={restoreSnapshot}
        open={changesOpen}
        panelId={panel.id}
      />
      <ExportDialog jobs={model.exports} onClose={() => setExportOpen(false)} open={exportOpen} requestId={model.result?.request_id} />
      <SettingsDrawer onClose={() => setSettingsOpen(false)} open={settingsOpen} />
    </div>
  );
}
