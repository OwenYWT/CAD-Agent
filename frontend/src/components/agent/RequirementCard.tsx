import { useState } from "react";
import type { RequirementBasis } from "../../types/requirements";
import type { ManufacturingProfile } from "../../types";

export function RequirementSummary({ objective, basis, profile, compact = false }: {
  compact?:boolean; objective:string; basis?:RequirementBasis | null; profile?:ManufacturingProfile | null;
}) {
  if (compact) return <section className="ww-requirement-card" aria-label="用户需求">
    <h3>用户需求</h3><p data-i18n-skip>{basis?.target || objective}</p>
    <p className="type-caption text-amber-800">{!basis || basis.source_kind === "none" ? "概念外形，适配未验证。" : "依据已记录，适配未验证。"}</p>
    <details><summary>条件、默认值与尺寸依据</summary><RequirementSummary objective={objective} basis={basis} profile={profile} /></details>
  </section>;
  return <section className="ww-requirement-card" aria-label="需求卡">
    <h3>需求依据</h3>
    <dl><div><dt>已确认条件</dt><dd data-i18n-skip>{basis?.target || objective || "等待描述目标"}</dd></div>
      <div><dt>用途</dt><dd>{basis?.purpose || "待确认"}</dd></div>
      <div><dt>尺寸来源</dt><dd>{basis?.source_reference || "未提供"}{basis?.source_kind && basis.source_kind!=='none' ? "（用户提供，未独立核验）" : ""}</dd></div>
      {basis?.dimensions ? <div><dt>明确尺寸</dt><dd data-i18n-skip>{basis.dimensions}</dd></div> : null}
      <div><dt>采用的制造设置</dt><dd>{profile ? `${profile.material} · ${profile.process.toUpperCase()}` : "待确认"}</dd></div>
      <div><dt>开孔与装配依据</dt><dd>{basis?.fit_notes || "未提供；开孔位置、装配间隙和实物配合未验证"}</dd></div>
    </dl>
    <p className="type-caption text-amber-800">{!basis || basis.source_kind==='none' ? '概念外形，适配未验证。' : '依据已记录，适配未验证。'}模型生成成功、检查通过和保存版本分别确认。</p>
  </section>;
}

export default function RequirementCard({ objective, profile, onConfirm, onBack, disabled=false }: {
  objective:string; profile?:ManufacturingProfile | null; onConfirm:(basis:RequirementBasis)=>void; onBack:()=>void; disabled?:boolean;
}) {
  const [basis,setBasis]=useState<RequirementBasis>({schema_version:'requirement-basis.v1',target:objective.slice(0,1000),purpose:'',source_kind:'none',source_reference:'',dimensions:'',fit_notes:'',concept_acknowledged:false});
  const set=(patch:Partial<RequirementBasis>)=>setBasis(value=>({...value,...patch}));
  const complete=basis.source_kind!=='none' && !!basis.dimensions.trim() && !!basis.source_reference.trim();
  return <section className="ww-requirement-card" aria-label="执行前需求确认" data-task-phase="needs_input">
    <h3>等待补充 · 确认建模依据</h3>
    <p className="type-caption">只补充影响几何和适配的依据。请核对下方制造设置；下列信息会随任务保存。</p>
    <label>目标<input aria-label="建模目标" maxLength={1000} value={basis.target} onChange={e=>set({target:e.target.value})} /></label>
    <label>用途（可选）<input aria-label="模型用途" maxLength={500} value={basis.purpose} placeholder="例如外观评估、实际保护或装配试制" onChange={e=>set({purpose:e.target.value})} /></label>
    <label>尺寸依据<select aria-label="尺寸依据" value={basis.source_kind} onChange={e=>set({source_kind:e.target.value as RequirementBasis['source_kind']})}>
      <option value="none">尚无可靠尺寸依据</option><option value="user_measurement">用户实测</option><option value="reference">用户提供的图纸或参考资料</option>
    </select></label>
    {basis.source_kind!=='none' ? <><label>来源说明<input aria-label="尺寸来源说明" value={basis.source_reference} maxLength={500} placeholder="测量对象与方法，或图纸名称、页码、链接" onChange={e=>set({source_reference:e.target.value})} /></label>
      <label>关键尺寸<textarea aria-label="关键尺寸依据" value={basis.dimensions} maxLength={1500} rows={2} placeholder="明确数值和单位；不要只填写手机型号" onChange={e=>set({dimensions:e.target.value})} /></label></> : null}
    <details><summary>开孔、装配间隙等关键依据</summary><textarea aria-label="开孔与装配依据" value={basis.fit_notes} maxLength={1000} rows={2} placeholder="例如相机开孔位置与尺寸、装配间隙；没有资料时保留未验证" onChange={e=>set({fit_notes:e.target.value})} /></details>
    <RequirementSummary objective={objective} basis={basis} profile={profile} />
    {basis.source_kind==='none' ? <label className="ww-requirement-consent"><input aria-label="确认仅生成概念外形" type="checkbox" checked={basis.concept_acknowledged} onChange={e=>set({concept_acknowledged:e.target.checked})} />我确认仅生成概念外形，适配未验证</label> : null}
    <div className="mt-3 flex flex-wrap justify-end gap-2"><button className="workspace-button" onClick={onBack} type="button">返回修改</button>
      <button className="workspace-button workspace-button--primary" disabled={disabled || !basis.target.trim() || !(complete || basis.source_kind==='none' && basis.concept_acknowledged)} onClick={()=>onConfirm(basis)} type="button">{basis.source_kind==='none' ? '按概念外形继续' : '按已提供依据继续'}</button></div>
  </section>;
}
