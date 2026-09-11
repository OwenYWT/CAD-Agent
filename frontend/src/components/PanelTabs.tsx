import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n/I18nContext";
import { useSessionStore } from "../stores/sessionStore";
import { Icon } from "./ui/Icon";

/** Engineering conversations rendered in the existing project sidebar. */
export default function PanelTabs() {
  const { panels, activePanelId, switchPanel, addPanel, removePanel, renamePanel } =
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
      <button
        aria-label={translate("新建对话")}
        className="ww-conversation-create type-control"
        onClick={() => addPanel()}
        title={translate("新建对话")}
        type="button"
      >
        <span>{translate("新建对话")}</span>
        <Icon name="plus" size={16} />
      </button>

      <div className="ww-conversation-list" ref={listRef} role="list">
        {panels.map((panel) => {
          const isActive = panel.id === activePanelId;
          const isEditing = editingId === panel.id;

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
                  onClick={() => switchPanel(panel.id)}
                  onDoubleClick={() => startRename(panel.id, panel.title)}
                  type="button"
                >
                  <span className="ww-conversation-title">{translate(panel.title)}</span>
                </button>
              )}
              {panel.isGenerating ? <span aria-label={translate("正在处理")} className="ww-conversation-spinner" role="status" /> : null}
              {panels.length > 1 ? (
                <button
                  aria-label={translate(`关闭${panel.title}`)}
                  className="ww-conversation-close"
                  onClick={(event) => {
                    event.stopPropagation();
                    removePanel(panel.id);
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
