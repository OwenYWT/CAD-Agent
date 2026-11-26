import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n/I18nContext";
import { useSessionStore } from "../stores/sessionStore";
import { Icon } from "./ui/Icon";
import { guardDraft } from "../stores/draftGuard";

/** Engineering conversations rendered in the existing project sidebar. */
export default function PanelTabs({ onSelect }: { onSelect: () => void }) {
  const { panels, activePanelId, switchPanel, removePanel, renamePanel } =
    useSessionStore();
  const { translate } = useI18n();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const activeRowRef = useRef<HTMLDivElement>(null);

  const startRename = (id: string, currentTitle: string) => {
    setEditingId(id);
    setEditTitle(currentTitle);
  };

  const commitRename = () => {
    if (editingId && editTitle.trim()) {
      renamePanel(editingId, editTitle.trim());
    }
    setEditingId(null);
  };

  useEffect(() => {
    const list = listRef.current;
    const activeRow = activeRowRef.current;
    if (!list || !activeRow) return;

    const rowTop = activeRow.offsetTop;
    const rowBottom = rowTop + activeRow.offsetHeight;
    if (rowTop < list.scrollTop) {
      list.scrollTop = rowTop;
    } else if (rowBottom > list.scrollTop + list.clientHeight) {
      list.scrollTop = rowBottom - list.clientHeight;
    }
  }, [activePanelId, panels.length]);

  return (
    <nav aria-label={translate("工程线程")} className="ww-conversation-navigation">
      <div className="ww-conversation-list" ref={listRef} role="list">
        {panels.map((panel) => {
          const isActive = panel.id === activePanelId;
          const isEditing = editingId === panel.id;
          const firstRequest = panel.messages.find(message => message.role === "user")?.content;
          const title = /^对话\s*\d+$/.test(panel.title) && firstRequest ? firstRequest : translate(panel.title);

          return (
            <div
              className={`ww-conversation-row group ${isActive ? "is-active" : ""}`}
              key={panel.id}
              ref={isActive ? activeRowRef : undefined}
              role="listitem"
            >
              {isEditing ? (
                <input
                  aria-label={`${translate("重命名对话")}: ${panel.title}`}
                  autoFocus
                  className="ww-conversation-rename type-control"
                  onBlur={commitRename}
                  onChange={(event) => setEditTitle(event.target.value)}
                  onClick={(event) => event.stopPropagation()}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") commitRename();
                    if (event.key === "Escape") setEditingId(null);
                  }}
                  type="text"
                  value={editTitle}
                />
              ) : (
                <button
                  aria-current={isActive ? "page" : undefined}
                  className="ww-conversation-switch type-control"
                  onClick={() => guardDraft(() => { switchPanel(panel.id); onSelect(); })}
                  title={title} onDoubleClick={() => startRename(panel.id, panel.title)}
                  type="button"
                >
                  <span className="ww-conversation-title" data-i18n-skip>{title}</span>
                </button>
              )}
              {panel.isGenerating ? <span aria-label={translate("正在处理")} className="ww-conversation-spinner" role="status" /> : null}
              {panels.length > 1 ? (
                <button
                  aria-label={translate(`关闭${panel.title}`)}
                  className="ww-conversation-close"
                  onClick={(event) => {
                    event.stopPropagation();
                    guardDraft(() => removePanel(panel.id));
                  }}
                  title={translate("关闭")}
                  type="button"
                >
                  <Icon name="x" size={14} />
                </button>
              ) : null}
            </div>
          );
        })}
      </div>
    </nav>
  );
}
