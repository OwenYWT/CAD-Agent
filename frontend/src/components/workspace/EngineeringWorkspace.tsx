import { useCallback, useEffect, useMemo, useState } from "react";
import type { AuthUser } from "../../auth";
import { adaptEngineeringProject } from "../../adapters/projectAdapter";
import { restoreModelSnapshot } from "../../services/engineeringService";
import { useWebSocket } from "../../hooks/useWebSocket";
import { useSessionStore } from "../../stores/sessionStore";
import type { ManufacturingProfile, ModelSnapshotDetail } from "../../types";
import type {
  DurableChangeSetDetail,
  EngineeringDomain,
  EngineeringStage,
} from "../../types/engineering";
import AgentRunTimeline from "../AgentRunTimeline";
import AgentDrawer from "../agent/AgentDrawer";
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
  const { connectionState, sendMessage, executeCode, resumeRun, restoreContext, modifyPart } = useWebSocket();
  const model = useMemo(() => adaptEngineeringProject(sessionId, panel), [panel, sessionId]);
  const hasProject = panel.messages.length > 0 || panel.result !== null || panel.isGenerating;
  const [view, setView] = useState<EngineeringDomain>("overview");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebar, setMobileSidebar] = useState(false);
  const [startHistory, setStartHistory] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [agentPrompt, setAgentPrompt] = useState("");
  const [parametersOpen, setParametersOpen] = useState(false);
  const [checksOpen, setChecksOpen] = useState(false);
  const [changesOpen, setChangesOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);

  useEffect(() => {
    bindOwner(user.id);
  }, [bindOwner, user.id]);

  const startProject = (prompt: string, profile: ManufacturingProfile | null = null) => {
    if (!sendMessage(prompt, "auto", profile)) return false;
    useSessionStore.getState().beginGeneration();
    useSessionStore.getState().addMessage({ role: "user", content: prompt });
    setView("overview");
    return true;
  };

  const navigate = (next: string) => setView(next as EngineeringDomain);
  const openStage = (stage: EngineeringStage) => {
    if (!stage.available) return;
    if (stage.id === "requirements" && model.result?.design_brief) { setChecksOpen(true); return; }
    if (stage.id === "manufacturing") { setChecksOpen(true); return; }
    if (stage.id === "release") { setExportOpen(true); return; }
    if (stage.domain) setView(stage.domain);
  };
  const askAgent = (prompt = "") => { setAgentPrompt(prompt); setAgentOpen(true); };
  const executeWithProgress = (code: string) => {
    if (!executeCode(code)) return false;
    useSessionStore.getState().beginGeneration("\u6b63\u5728\u91cd\u65b0\u8ba1\u7b97\u6a21\u578b");
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
  const restoreSnapshot = async (snapshot: ModelSnapshotDetail) => {
    const restored = await restoreModelSnapshot(snapshot.id);
    const restoredRevisionId = restored.result.revision_id || restored.revision_id || undefined;
    const restoredResult = {
      ...restored.result,
      revision_id: restoredRevisionId,
      expected_base_revision_id: restoredRevisionId || restored.result.expected_base_revision_id,
      project_id: restored.result.project_id || restored.project_id,
      branch_id: restored.result.branch_id || restored.branch_id,
      files: restored.result.files || restored.files || {},
      params: restored.result.params || restored.params || undefined,
      parameters: restored.result.parameters || restored.parameters || undefined,
      validation: restored.result.validation || restored.validation || undefined,
      inspect_report: restored.result.inspect_report || restored.inspect_report || undefined,
      repair_history: restored.result.repair_history || restored.repair_history || undefined,
    };
    useSessionStore.getState().restorePanelResult(
      panel.id,
      restoredResult,
      restored.code,
    );
    setView("mechanical");
    return true;
  };

  if (ownerId !== user.id) {
    return <div className="grid min-h-screen place-items-center text-sm text-[var(--muted)]">{"\u6b63\u5728\u6062\u590d\u5de5\u7a0b\u4f1a\u8bdd..."}</div>;
  }

  if (!hasProject) {
    return <>
      <ProjectStart connectionState={connectionState} onOpenHistory={() => setStartHistory(true)} onStart={startProject} />
      {startHistory ? (
        <div className="start-history-overlay">
          <button aria-label="\u5173\u95ed\u5386\u53f2\u9879\u76ee" className="start-history-overlay__backdrop" onClick={() => setStartHistory(false)} type="button" />
          <ProjectSidebar activeView="overview" collapsed={false} mobileOpen onCollapse={() => {}} onMobileClose={() => setStartHistory(false)} onNavigate={() => setStartHistory(false)} restoreContext={restoreContext} />
        </div>
      ) : null}
    </>;
  }

  return (
    <div className="flex h-[100dvh] min-w-[320px] flex-col overflow-hidden bg-white text-[var(--ink)]">
      <WorkspaceHeader connection={connectionState} onAgent={() => askAgent()} onBack={() => setView("overview")} onChanges={() => setChangesOpen(true)} onChecks={() => setChecksOpen(true)} onExport={() => setExportOpen(true)} onLogout={onLogout} onMenu={() => setMobileSidebar(true)} onSettings={() => setSettingsOpen(true)} onUserUpdate={onUserUpdate} project={model.project} user={user} />
      <div className="flex min-h-0 flex-1">
        <ProjectSidebar activeView={view} collapsed={sidebarCollapsed} mobileOpen={mobileSidebar} onCollapse={() => setSidebarCollapsed((value) => !value)} onMobileClose={() => setMobileSidebar(false)} onNavigate={navigate} restoreContext={restoreContext} />
        <div className="min-w-0 flex-1 overflow-y-auto">
          <div className={view === "mechanical" ? "flex min-h-full min-w-0 flex-col gap-4 p-4 sm:p-6 xl:flex-row" : "min-w-0 p-4 sm:p-6"}>
            <div className={view === "mechanical" ? "min-w-0 flex-1 space-y-4" : "min-w-0 space-y-4"}>
              {(panel.activeRun || panel.stepHistory.length > 0 || panel.artifactUpdates.length > 0 || panel.durable?.agent) ? (
                <AgentRunTimeline
                  activeRun={panel.activeRun}
                  artifacts={panel.artifactUpdates}
                  durableAgent={panel.durable?.agent}
                  inspectReport={model.result?.inspect_report}
                  isGenerating={panel.isGenerating}
                  onRerunCode={() => { if (model.result?.code) executeWithProgress(model.result.code); }}
                  onResumeRun={resumeWithProgress}
                  onRetryPrompt={() => { if (latestUserPrompt) startProject(latestUserPrompt, model.result?.manufacturing_profile || null); }}
                  repairHistory={model.result?.repair_history}
                  result={model.result}
                  steps={panel.stepHistory}
                />
              ) : null}
              {view === "overview" ? <ProjectFlow onOpenStage={openStage} project={model.project} stages={model.stages} task={model.task} /> : null}
              {view === "mechanical" ? <MechanicalWorkspace currentStep={panel.currentStep} isGenerating={panel.isGenerating} onAgent={() => askAgent()} onBack={() => setView("overview")} onProperties={() => setParametersOpen(true)} result={model.result} /> : null}
            </div>
            {view === "mechanical" ? (
              <div className="min-w-0 w-full shrink-0 xl:sticky xl:top-4 xl:w-[380px] xl:self-start">
                <VersionHistoryPanel
                  activeSnapshotId={model.result?.snapshot_id}
                  currentParts={model.result?.assembly_parts || []}
                  onModifyPart={modifyPart}
                  onRestore={restoreSnapshot}
                  panelId={panel.id}
                  refreshKey={model.result?.snapshot_id}
                />
              </div>
            ) : null}
          </div>
        </div>
      </div>
      <button aria-label="\u8be2\u95ee Agent" className="fixed bottom-4 right-4 z-30 grid h-12 w-12 place-items-center rounded-full bg-[var(--ink)] text-white shadow-xl sm:hidden" onClick={() => askAgent()} type="button"><Icon name="message" size={18} /></button>

      <ParameterDrawer isGenerating={panel.isGenerating} key={`parameters:${model.result?.request_id || "empty"}:${parametersOpen}`} onClose={() => setParametersOpen(false)} onExecute={executeWithProgress} open={parametersOpen} parameters={model.parameters} result={model.result} />
      <AgentDrawer connection={connectionState} context={view} key={`${view}:${agentPrompt}:${agentOpen}`} onClose={() => { setAgentOpen(false); setAgentPrompt(""); }} onSend={sendMessage} open={agentOpen} suggestedPrompt={agentPrompt} />
      <ValidationDialog
        activeSnapshotId={model.result?.snapshot_id}
        activeRun={panel.activeRun}
        artifacts={panel.artifactUpdates}
        durableAgent={panel.durable?.agent}
        description={latestUserPrompt}
        isGenerating={panel.isGenerating}
        key={"validation:" + (model.result?.request_id || "empty") + ":" + checksOpen}
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
