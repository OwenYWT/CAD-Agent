import type { SemanticFeature } from "../types/document";

export function featureTreeRows(features: SemanticFeature[], status?: string, rootOrder: string[] = []) {
  const visible = features.filter(f => f.kernel_name !== "CADAgentLedger");
  const flat = {hierarchyAvailable:false, rows:visible.map(feature => ({feature, level:1, bodyTip:false}))};
  if (status !== "measured" || visible.some(f => !f.structure || f.structure.container_ids.length > 1)) return flat;
  const byId = new Map(visible.map(f => [f.id, f]));
  const roots = visible.filter(f => !f.structure!.container_ids.some(id => byId.has(id)));
  roots.sort((a,b) => {
    const ai = rootOrder.indexOf(a.id), bi = rootOrder.indexOf(b.id);
    return (ai < 0 ? rootOrder.length : ai) - (bi < 0 ? rootOrder.length : bi);
  });
  const visited = new Set<string>();
  const rows: typeof flat.rows = [];
  let valid = true;
  function visit(feature: SemanticFeature, level: number, bodyTip = false) {
    if (visited.has(feature.id) || level > 32) { valid = false; return; }
    visited.add(feature.id); rows.push({feature,level,bodyTip});
    for (const id of feature.structure!.member_ids) {
      const member = byId.get(id);
      if (!member) { valid = false; continue; }
      if (member.structure!.container_ids[0] !== feature.id) valid = false;
      visit(member,level + 1,feature.structure!.body_tip_id === id);
    }
  }
  roots.forEach(f => visit(f,1));
  return valid && visited.size === visible.length ? {hierarchyAvailable:true,rows} : flat;
}
