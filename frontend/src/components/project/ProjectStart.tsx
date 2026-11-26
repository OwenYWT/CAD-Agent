import RequirementCard from "../agent/RequirementCard";
import ChatComposer from "../agent/ChatComposer";
import type { RequirementBasis } from "../../types/requirements";
import { useId, useRef, useState } from "react";
import type { ConnectionState } from "../../hooks/useWebSocket";
import type { ManufacturingProfile } from "../../types";
import { MANUFACTURING_PROFILE_PRESETS, getManufacturingProfilePreset } from "../../utils/manufacturingProfiles";
import { BrandMark } from "../common/BrandMark";
import { Icon } from "../ui/Icon";
import NativeImportPanel from './NativeImportPanel';
import type { DurableTaskSubmittedEvent } from '../../types';

const EXAMPLES = [
  ["传感器外壳", "设计一个带 M4 安装孔的防水传感器外壳，适合 FDM 打印"],
  ["两腔电子盒", "创建一个 60 × 40 × 25 mm 的两腔电子盒，壁厚 2 mm"],
  ["桌边耳机挂钩", "设计一个可夹在 25 mm 桌板上的耳机挂钩"],
];

interface ProjectStartProps {
  connectionState: ConnectionState;
  onStart: (prompt: string, profile: ManufacturingProfile, basis?: RequirementBasis) => boolean;
  onOpenHistory: () => void;
  embedded?: boolean;
  importContext?:{sessionId:string;panelId:string;onImported:(receipt:DurableTaskSubmittedEvent)=>void};
}

export default function ProjectStart({ connectionState, onStart, onOpenHistory, embedded = false, importContext }: ProjectStartProps) {
  const [prompt, setPrompt] = useState("");
  const [profileId, setProfileId] = useState(MANUFACTURING_PROFILE_PRESETS[0].id);
  const [reviewing, setReviewing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const promptField = useRef<HTMLTextAreaElement>(null);
  const profile = getManufacturingProfilePreset(profileId).profile;
  const importKey = importContext ? `cad-native-import:${importContext.sessionId}:${importContext.panelId}` : null;
  const [importOpen, setImportOpen] = useState(() => Boolean(importKey && sessionStorage.getItem(importKey)));
  const importId = useId();

  const start = (basis?: RequirementBasis) => {
    const value = (basis?.target || prompt).trim();
    if (!value) return;
    if (!basis) { setError(null); setReviewing(true); return; }
    if (!onStart(value, profile, basis)) {
      setError("实时连接尚未就绪，需求已保留。请稍后重试。");
      return;
    }
    setError(null);
  };

  const content = (
    <main className={`ww-start ${reviewing ? "ww-start--review" : ""}`}>
      <div className="ww-start-content">
        {reviewing ? (
          <>
            <div className="ww-request-message" aria-label="用户需求"><p data-i18n-skip>{prompt}</p></div>
            <RequirementCard objectiveInThread objective={prompt} profile={profile} onConfirm={start}
              onBack={() => { setError(null); setReviewing(false); }} disabled={connectionState !== "connected"} />
          </>
        ) : (
          <>
            <h1 className="type-display-heading">你想设计什么？</h1>
            <ChatComposer label="工程需求" submitLabel="开始创建" placeholder="描述你的设计…" value={prompt}
              onChange={setPrompt} sendDisabled={connectionState !== "connected"} onSubmit={() => start()} inputRef={promptField} maxLength={4000} autoFocus
              tools={<>
                {importContext ? <button type="button" className="ww-chat-tool" aria-label="打开已有 FCStd / STEP" title="打开已有 FCStd / STEP"
                  aria-expanded={importOpen} aria-controls={importId} onClick={() => setImportOpen(open => !open)}><Icon name="plus" size={20}/></button> : null}
                <select aria-label="制造方式" title="制造方式" className="ww-chat-profile" value={profileId} onChange={event => setProfileId(event.target.value)}>
                  {MANUFACTURING_PROFILE_PRESETS.map(preset => <option key={preset.id} value={preset.id}>{preset.label}</option>)}
                </select>
              </>} />
            <div className="ww-start-examples" role="group" aria-label="需求示例">
              {EXAMPLES.map(([label, example]) => <button key={label} type="button" aria-label={example} title={example}
                onClick={() => { setPrompt(example); promptField.current?.focus(); }}>{label}</button>)}
            </div>
          </>
        )}
        {importContext && importKey ? <div id={importId} hidden={reviewing || !importOpen}><NativeImportPanel {...importContext} storageKey={importKey} profile={profile} /></div> : null}
        {connectionState !== "connected" ? <p className="ww-start-connection type-caption" role="status">{connectionState === "disconnected" ? "实时连接已断开" : "正在连接实时任务"}</p> : null}
        {error ? <p className="ww-agent-error" role="alert">{error}</p> : null}
      </div>
    </main>
  );

  if (embedded) return content;

  return (
    <div className="flex h-[100dvh] min-w-[320px] flex-col bg-[var(--canvas)] text-[var(--ink)]">
      <header className="flex h-[56px] shrink-0 items-center px-5 sm:px-7">
        <BrandMark className="type-section-heading" />
        <button className="workspace-button ml-auto" onClick={onOpenHistory} type="button"><Icon name="history" size={15} />历史项目</button>
      </header>
      {content}
    </div>
  );
}
