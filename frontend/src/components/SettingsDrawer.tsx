import { useEffect, useRef, useState } from "react";
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
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const drawer = drawerRef.current;
      if (!drawer) return;
      const focusable = Array.from(
        drawer.querySelectorAll<HTMLElement>(
          "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])",
        ),
      ).filter((element) => element.getAttribute("aria-hidden") !== "true");
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !drawer.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      previouslyFocused?.focus();
    };
  }, [onClose, open]);

  if (!open) return null;

  return (
    <>
      <button
        aria-label="关闭设置"
        className="ww-settings-backdrop"
        onClick={onClose}
        tabIndex={-1}
        type="button"
      />

      <aside aria-labelledby="settings-title" aria-modal="true" className="ww-settings-drawer" ref={drawerRef} role="dialog">
        <div className="ww-settings-header">
          <div>
            <p className="ww-pane-eyebrow">Workspace</p>
            <h2 id="settings-title">设置</h2>
          </div>
          <div className="flex items-center gap-2">
            <LanguageSwitch className="workspace-button ww-language-switch" />
            <button
              aria-label="关闭设置"
              onClick={onClose}
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
            onClick={() => setTab("rules")}
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
            onClick={() => setTab("knowledge")}
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
            onClick={() => setTab("capabilities")}
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
            onClick={() => setTab("runner")}
            role="tab"
            type="button"
          >
            运行能力
          </button>
        </div>

        <div className="ww-settings-content">
          {tab === "rules" && <div aria-labelledby="settings-tab-rules" id="settings-panel-rules" role="tabpanel"><DFMRuleConfigInline /></div>}
          {tab === "knowledge" && <div aria-labelledby="settings-tab-knowledge" id="settings-panel-knowledge" role="tabpanel"><KnowledgeGraphInline /></div>}
          {tab === "capabilities" && <div aria-labelledby="settings-tab-capabilities" id="settings-panel-capabilities" role="tabpanel"><CapabilityCatalog /></div>}
          {tab === "runner" && <div aria-labelledby="settings-tab-runner" id="settings-panel-runner" role="tabpanel"><CapabilityRunner /></div>}
        </div>
      </aside>
    </>
  );
}

/** DFMRuleConfig always-open variant for settings drawer */
function DFMRuleConfigInline() {
  return (
    <div className="p-0">
      <DFMRuleConfigAlwaysOpen />
    </div>
  );
}

/** KnowledgeGraph always-open variant for settings drawer */
function KnowledgeGraphInline() {
  return (
    <div className="p-0">
      <KnowledgeGraphAlwaysOpen />
    </div>
  );
}

function DFMRuleConfigAlwaysOpen() {
  return <DFMRuleConfig />;
}

function KnowledgeGraphAlwaysOpen() {
  return <KnowledgeGraph />;
}
