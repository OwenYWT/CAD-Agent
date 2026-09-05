import { useState } from "react";
import type { ConnectionState } from "../../hooks/useWebSocket";
import type { ManufacturingProfile } from "../../types";
import { MANUFACTURING_PROFILE_PRESETS, getManufacturingProfilePreset } from "../../utils/manufacturingProfiles";
import { BrandMark } from "../common/BrandMark";
import { Icon } from "../ui/Icon";

const EXAMPLES = [
  "设计一个带 M4 安装孔的防水传感器外壳，适合 FDM 打印",
  "创建一个 60 × 40 × 25 mm 的两腔电子盒，壁厚 2 mm",
  "设计一个可夹在 25 mm 桌板上的耳机挂钩",
];

interface ProjectStartProps {
  connectionState: ConnectionState;
  onStart: (prompt: string, profile: ManufacturingProfile) => boolean;
  onOpenHistory: () => void;
  embedded?: boolean;
}

export default function ProjectStart({ connectionState, onStart, onOpenHistory, embedded = false }: ProjectStartProps) {
  const [prompt, setPrompt] = useState("");
  const [profileId, setProfileId] = useState(MANUFACTURING_PROFILE_PRESETS[0].id);
  const [error, setError] = useState<string | null>(null);

  const start = () => {
    const value = prompt.trim();
    if (!value) return;
    if (!onStart(value, getManufacturingProfilePreset(profileId).profile)) {
      setError("实时连接尚未就绪，需求已保留。请稍后重试。");
      return;
    }
    setError(null);
  };

  const content = (
    <main className="grid min-h-0 flex-1 place-items-center overflow-y-auto px-5 pb-16 pt-8">
        <div className="w-full max-w-[780px]">
          <div className="text-center">
            <h1 className="type-display-heading   tracking-[-0.045em]">今天要创建什么工程？</h1>
          </div>

          <div className="mt-8 rounded-xl border border-[var(--line-strong)] bg-white p-3 shadow-[0_10px_28px_rgba(35,35,31,0.06)] focus-within:border-[var(--focus)]">
            <textarea aria-label="工程需求" autoFocus className="min-h-32 w-full resize-none border-0 bg-transparent px-2 py-2 type-control  text-[var(--ink)] outline-none placeholder:text-[var(--faint)]" onChange={(event) => setPrompt(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) start(); }} placeholder="例如：设计一个 IP67 工业传感器外壳，支持壁装，内部预留 PCB、电池和密封圈空间……" value={prompt} />
            <div className="flex flex-wrap items-center gap-2 border-t border-[var(--line)] px-1 pt-3">
              <span className="type-caption text-[var(--faint)]">当前生成接口支持文本需求；能力工作区可单独上传工程文件。</span>
              <label className="flex items-center gap-1.5 type-control text-[var(--muted)]"><span>制造方式</span><select aria-label="制造方式" className="rounded-md border border-[var(--line)] bg-white px-2 py-1 outline-none focus:border-[var(--focus)]" onChange={(event) => setProfileId(event.target.value)} value={profileId}>{MANUFACTURING_PROFILE_PRESETS.map((preset) => <option key={preset.id} value={preset.id}>{preset.label}</option>)}</select></label>
              {connectionState !== "connected" ? <span className="type-caption text-amber-700">正在连接实时任务</span> : null}
              <button className="workspace-button workspace-button--primary ml-auto" disabled={!prompt.trim() || connectionState !== "connected"} onClick={start} type="button">开始创建<Icon name="chevron-right" size={15} /></button>
            </div>
          </div>
          {error ? <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 type-body text-red-800" role="alert">{error}</div> : null}

          <div className="mt-4 grid gap-2 sm:grid-cols-3">
            {EXAMPLES.map((example) => <button className="min-h-20 rounded-lg border border-[var(--line)] bg-white px-4 py-3 text-left type-control  text-[var(--muted)] transition hover:border-[var(--line-strong)] hover:text-[var(--ink)]" key={example} onClick={() => setPrompt(example)} type="button">{example}</button>)}
          </div>
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
