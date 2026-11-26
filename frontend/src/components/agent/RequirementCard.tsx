import { initialRequirementBasis, requirementNotice } from "../../adapters/requirementIntake";
import { useEffect, useRef, useState } from "react";
import { previewRequirements } from "../../services/clients/tasks";
import type { RequirementBasis } from "../../types/requirements";
import type { ManufacturingProfile } from "../../types";

export function RequirementSummary({ objective, basis, profile, compact = false }: {
  compact?:boolean; objective:string; basis?:RequirementBasis | null; profile?:ManufacturingProfile | null;
}) {
  if (compact) return <section className="ww-request-message" aria-label="用户需求">
    <p data-i18n-skip>{basis?.target || objective}</p>
    {basis?.concept_acknowledged || basis?.design_scope !== "geometry" || basis.source_kind === "none"
      ? <small className="block pt-1 type-caption text-amber-800">{requirementNotice(basis)}</small> : null}
    <details><summary>条件、默认值与尺寸依据</summary><RequirementSummary objective={objective} basis={basis} profile={profile} /></details>
  </section>;
  return <section className="ww-requirement-card" aria-label="需求卡">
    <h3>需求依据</h3>
    <dl><div><dt>已确认条件</dt><dd>{basis?.target || objective ? <span data-i18n-skip>{basis?.target || objective}</span> : "等待描述目标"}</dd></div>
      {basis?.purpose || basis?.design_scope !== "geometry" ? <div><dt>用途</dt><dd>{basis?.purpose ? <span data-i18n-skip>{basis.purpose}</span> : "待确认（不影响本次外形时可后补）"}</dd></div> : null}
      <div><dt>尺寸来源</dt><dd>{basis?.source_kind==='user_specification' && ['用户指定的设计尺寸','用户需求中指定的设计尺寸'].includes(basis.source_reference) ? "用户指定的设计尺寸" : basis?.source_reference ? <span data-i18n-skip>{basis.source_reference}</span> : "未提供"}{basis?.source_kind && ['reference','user_measurement'].includes(basis.source_kind) ? "（用户提供，未独立核验）" : ""}</dd></div>
      {basis?.dimensions ? <div><dt>明确尺寸</dt><dd data-i18n-skip>{basis.dimensions}</dd></div> : null}
      <div><dt>采用的制造设置</dt><dd>{profile ? `${profile.material} · ${profile.process.toUpperCase()}` : "待确认"}</dd></div>
      {basis?.design_scope !== "geometry" ? <div><dt>开孔与装配依据</dt><dd>{basis?.fit_notes ? <span data-i18n-skip>{basis.fit_notes}</span> : "未提供；开孔位置、装配间隙和实物配合未验证"}</dd></div> : null}
    </dl>
    <p className="type-caption text-amber-800">{requirementNotice(basis)}模型生成成功、检查通过和保存版本分别确认。</p>
  </section>;
}

export default function RequirementCard({ objective, profile, onConfirm, onBack, disabled=false, initialBasis, objectiveInThread=false }: {
  objective:string; profile?:ManufacturingProfile | null; onConfirm:(basis:RequirementBasis)=>void; onBack:()=>void; disabled?:boolean; initialBasis?:RequirementBasis; objectiveInThread?:boolean;
}) {
  const [basis,setBasis]=useState<RequirementBasis>(()=>initialBasis ? {...initialBasis,target:objective} : initialRequirementBasis(objective));
  const edited = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const [previewError, setPreviewError] = useState('');
  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => {
    if (initialBasis) return;
    const controller = new AbortController();
    previewRequirements(objective, controller.signal).then(preview => {
      if (!controller.signal.aborted && !edited.current) setBasis(initialRequirementBasis(objective, preview));
    }).catch(() => {
      if (!controller.signal.aborted && !edited.current) setPreviewError('需求读取失败，可手动填写尺寸后继续。');
    });
    return () => controller.abort();
  }, [objective, initialBasis]);
  const physical = basis.design_scope !== "geometry";
  const set=(patch:Partial<RequirementBasis>)=>{
    edited.current = true;
    setPreviewError('');
    setBasis(value=>({...value,...patch}));
  };
  const complete=basis.source_kind!=='none' && !!basis.dimensions.trim() && !!basis.source_reference.trim();
  return <section className="ww-requirement-card ww-intake" aria-label="执行前需求确认" data-task-phase="needs_input" onFocusCapture={event=>{
    if (!edited.current && event.target.matches('input, textarea, select')) {
      edited.current = true;
      // Start from the visible render even if a preview update is already queued.
      setBasis(basis);
    }
  }}>
    <h2 className="type-section-heading" ref={heading} tabIndex={-1}>{complete ? "确认建模条件" : "补充建模条件"}</h2>
    {!objectiveInThread && !basis.dimensions.trim() && basis.target ? <p className="whitespace-pre-wrap" data-i18n-skip>{basis.target}</p> : null}
    {previewError ? <p role="status">{previewError}</p> : null}
    <label>关键尺寸<textarea aria-label="关键尺寸依据" value={basis.dimensions} maxLength={1500} rows={2} placeholder={physical ? "明确数值和单位；不要只填写手机型号" : "例如长 100、宽 60、厚 3 mm"} onChange={e=>set({dimensions:e.target.value,...(!physical ? {source_kind:e.target.value.trim() ? 'user_specification' : 'none',source_reference:e.target.value.trim() ? '用户指定的设计尺寸' : ''} : {})})} /></label>
    <details className="ww-intake-goal"><summary>核对或修改建模目标</summary>
      <label>目标<textarea aria-label="建模目标" maxLength={4000} rows={2} value={basis.target} onChange={e=>set({target:e.target.value})} /></label>
      <p className="type-caption">修改尺寸后，请一并核对目标中的数值和条件。</p>
    </details>
    <div className="ww-intake-settings">
      <label>建模用途<select aria-label="建模范围" value={basis.design_scope} onChange={e=>set({design_scope:e.target.value as RequirementBasis['design_scope'],source_kind:'none',source_reference:'',concept_acknowledged:false})}><option value="geometry">几何设计（按指定尺寸创建）</option><option value="physical_fit">与实物适配或装配</option></select></label>
      {profile ? <div><span>制造方式</span><p>{profile.process === "generic" ? "通用制造" : `${profile.process.toUpperCase()} · ${profile.material}`}</p></div> : null}
    </div>
    {physical ? <>
      <label>尺寸依据<select aria-label="尺寸依据" value={basis.source_kind} onChange={e=>set({source_kind:e.target.value as RequirementBasis['source_kind']})}>
        <option value="none">尚无可靠尺寸依据</option><option value="user_measurement">用户实测</option><option value="reference">用户提供的图纸或参考资料</option>
      </select></label>
      {basis.source_kind!=='none' ? <label>来源说明<input aria-label="尺寸来源说明" value={basis.source_reference} maxLength={500} placeholder="测量对象与方法，或图纸名称、页码、链接" onChange={e=>set({source_reference:e.target.value})} /></label> : null}
    </> : null}
    {physical ? <details><summary>开孔、装配间隙等关键依据</summary><textarea aria-label="开孔与装配依据" value={basis.fit_notes} maxLength={1000} rows={2} placeholder="例如相机开孔位置与尺寸、装配间隙；没有资料时保留未验证" onChange={e=>set({fit_notes:e.target.value})} /></details> : null}
    {physical ? <p className="type-caption text-amber-800">{requirementNotice(basis)}</p> : null}
    <details><summary>查看本次需求依据</summary><RequirementSummary objective={basis.target} basis={basis} profile={profile} /></details>
    <label className="ww-requirement-consent"><input aria-label="确认仅生成概念外形" type="checkbox" checked={basis.concept_acknowledged} onChange={e=>set({concept_acknowledged:e.target.checked})} />{basis.source_kind==='none' ? "同意先生成概念外形，缺失尺寸待确认" : "仅验收概念几何，实物适配及性能另行验证"}</label>
    <div className="mt-3 flex flex-wrap justify-end gap-2"><button className="workspace-button" onClick={onBack} type="button">返回修改</button>
      <button className="workspace-button workspace-button--primary" disabled={disabled || !basis.target.trim() || !(complete || basis.source_kind==='none' && basis.concept_acknowledged)} onClick={()=>onConfirm(basis)} type="button">{basis.source_kind==='none' || basis.concept_acknowledged ? '按概念外形继续' : '确认并执行'}</button></div>
  </section>;
}
