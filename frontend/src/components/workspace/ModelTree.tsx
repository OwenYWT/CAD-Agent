import { useMemo, useState } from "react";
import { featureTreeRows, visibleFeatureRows } from "../../adapters/featureTree";
import type { CloudDocument } from "../../types/document";
import { Icon } from "../ui/Icon";

export default function ModelTree({ document, selectedId, onSelect }: { document: CloudDocument; selectedId?: string | null; onSelect: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const tree = useMemo(() => featureTreeRows(document.features, document.hierarchy_status, document.roots), [document]);
  const rows = useMemo(() => visibleFeatureRows(tree.rows,collapsed,query), [tree,query,collapsed]);
  const toggle = (id: string) => setCollapsed(previous => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  return <section className="ww-model-tree" aria-label="文档模型树">
    <h3>模型结构</h3><input className="ww-field" aria-label="搜索模型特征" placeholder="搜索名称或类型" value={query} onChange={event => setQuery(event.target.value)} />
    {!tree.hierarchyAvailable && document.features.length ? <p className="type-caption">此版本未记录完整容器层级，按特征列表显示。</p> : null}
    <div role="tree" aria-label="模型特征树" className="ww-model-tree-list">
      {rows.map(({ feature, level, bodyTip }) => <div key={feature.id} className="flex items-center" style={{ paddingLeft: (level - 1) * 14 }}>
        {feature.structure?.member_ids.length ? <button className="workspace-icon-button shrink-0" type="button" aria-label={`${collapsed.has(feature.id) ? "展开" : "收起"} ${feature.label}`} aria-expanded={!collapsed.has(feature.id)} onClick={() => toggle(feature.id)}><Icon name={collapsed.has(feature.id) ? "chevron-right" : "chevron-down"} size={14} /></button> : <Icon name="box" size={14} className="mx-2 shrink-0" />}
        <button role="treeitem" aria-label={feature.label} aria-level={level} aria-selected={feature.id === selectedId} aria-expanded={feature.structure?.member_ids.length ? !collapsed.has(feature.id) : undefined}
          className="workspace-button min-w-0 flex-1 justify-start" aria-pressed={feature.id === selectedId} type="button" onClick={() => onSelect(feature.id)}>
          <span className="truncate" data-i18n-skip title={feature.label}>{feature.label}</span>{bodyTip ? <span className="type-caption text-[var(--muted)]">最终特征</span> : null}
          <span className="sr-only">{feature.is_valid === true ? "有效" : feature.is_valid === false ? "无效" : "状态未知"}</span>
        </button>
      </div>)}
      {!rows.length ? <p>{query ? "没有匹配的特征" : "当前没有原生特征"}</p> : null}
    </div>
  </section>;
}
