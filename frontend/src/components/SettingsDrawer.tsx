import { createPortal } from "react-dom";
import { useModalFocus } from "../hooks/useModalFocus";
import { guardDraft } from "../stores/draftGuard";
import { useRef, useState } from "react";
import DFMRuleConfig from "./DFMRuleConfig";
import KnowledgeGraph from "./KnowledgeGraph";
import CapabilityCatalog from "./CapabilityCatalog";
import CapabilityRunner from "./CapabilityRunner";
import { Icon } from "./ui/Icon";
import { LanguageSwitch } from "../i18n/LanguageSwitch";

interface SettingsDrawerProps {
  open: boolean;
  onClose: () => void;
}

type SettingsTab = "rules" | "knowledge" | "capabilities" | "runner";

export default function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  const [tab, setTab] = useState<SettingsTab>("rules");
  const [visited, setVisited] = useState<Set<SettingsTab>>(() => new Set(["rules"]));
  const [recommendationDraft, setRecommendationDraft] = useState({ dimension: "", material: "" });
  const selectTab = (next: SettingsTab) => guardDraft(() => {
    setVisited(previous => new Set([...previous, next])); setTab(next);
  });
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);

  useModalFocus(open, drawerRef, () => { guardDraft(onClose); });

  if (!open) return null;

  return createPortal(
    <>
      <button
        aria-label="关闭设置"
        className="ww-settings-backdrop"
        onClick={() => guardDraft(onClose)}
        tabIndex={-1}
        type="button"
      />

      <aside aria-labelledby="settings-title" aria-modal="true" className="ww-settings-drawer" ref={drawerRef} role="dialog" tabIndex={-1}>
        <div className="ww-settings-header">
          <div>
            <p className="ww-pane-eyebrow">Workspace</p>
            <h2 id="settings-title">设置</h2>
          </div>
          <div className="flex items-center gap-2">
            <LanguageSwitch className="workspace-button ww-language-switch" />
            <button
              aria-label="关闭设置"
              onClick={() => guardDraft(onClose)}
              className="workspace-icon-button"
              title="关闭"
              type="button"
              ref={closeButtonRef}
            >
              <Icon name="x" size={18} />
            </button>
          </div>
        </div>

        <div aria-label="设置分类" className="ww-settings-tabs" role="tablist">
          <button
            aria-controls="settings-panel-rules"
            aria-selected={tab === "rules"}
            onClick={() => selectTab("rules")}
            className={`ww-settings-tab ${tab === "rules" ? "is-active" : ""}`}
            id="settings-tab-rules"
            role="tab"
            type="button"
          >
            DFM 规则
          </button>
          <button
            aria-controls="settings-panel-knowledge"
            aria-selected={tab === "knowledge"}
            onClick={() => selectTab("knowledge")}
            className={`ww-settings-tab ${tab === "knowledge" ? "is-active" : ""}`}
            id="settings-tab-knowledge"
            role="tab"
            type="button"
          >
            工艺知识图谱
          </button>
          <button
            aria-controls="settings-panel-capabilities"
            aria-selected={tab === "capabilities"}
            className={`ww-settings-tab ${tab === "capabilities" ? "is-active" : ""}`}
            id="settings-tab-capabilities"
            onClick={() => selectTab("capabilities")}
            role="tab"
            type="button"
          >
            全部能力
          </button>
          <button
            aria-controls="settings-panel-runner"
            aria-selected={tab === "runner"}
            className={`ww-settings-tab ${tab === "runner" ? "is-active" : ""}`}
            id="settings-tab-runner"
            onClick={() => selectTab("runner")}
            role="tab"
            type="button"
          >
            运行能力
          </button>
        </div>

        <div className="ww-settings-content">
          {visited.has("rules") && <div hidden={tab !== "rules"} aria-labelledby="settings-tab-rules" id="settings-panel-rules" role="tabpanel"><DFMRuleConfig embedded /></div>}
          {visited.has("knowledge") && <div hidden={tab !== "knowledge"} aria-labelledby="settings-tab-knowledge" id="settings-panel-knowledge" role="tabpanel"><KnowledgeGraph embedded recommendationDraft={recommendationDraft} onRecommendationDraft={setRecommendationDraft} /></div>}
          {visited.has("capabilities") && <div hidden={tab !== "capabilities"} aria-labelledby="settings-tab-capabilities" id="settings-panel-capabilities" role="tabpanel"><CapabilityCatalog /></div>}
          {visited.has("runner") && <div hidden={tab !== "runner"} aria-labelledby="settings-tab-runner" id="settings-panel-runner" role="tabpanel"><CapabilityRunner /></div>}
        </div>
      </aside>
    </>, document.body
  );
}
