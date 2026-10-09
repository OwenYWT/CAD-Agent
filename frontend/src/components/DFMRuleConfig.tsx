import { useEffect, useRef, useState } from "react";
import type { DFMRuleData } from "../types";
import { authFetch } from "../auth";
import { API_BASE, readJson } from "../services/clients/http";
import { guardDraft, useDraftGuardStore } from "../stores/draftGuard";
import { useMutationReceipt } from '../hooks/useMutationReceipt';

const PROCESS_LABELS: Record<string, string> = { CNC: "CNC 加工", FDM: "FDM 打印", SLA: "SLA 打印", injection_mold: "注塑成型", sheet_metal: "钣金加工" };
interface RuleSet { id: string; name: string; process: string; rules: DFMRuleData[] }

function RuleRow({ rule, onSaved }: { rule: DFMRuleData; onSaved: (rule: DFMRuleData) => void }) {
  // Server readback advances the baseline, never the user's later edits.
  const [draft, setDraft] = useState<Partial<Record<'threshold_min' | 'threshold_max', string>>>({});
  const min = draft.threshold_min ?? String(rule.threshold_min ?? "");
  const max = draft.threshold_max ?? String(rule.threshold_max ?? "");
  const edit = (field: 'threshold_min' | 'threshold_max', value: string) => setDraft(current => ({ ...current, [field]: value }));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const [uncertainUpdate, setUncertainUpdate] = useMutationReceipt<Record<string, number | boolean>>(`dfm-rule:${rule.id}`);
  const dirty = min !== String(rule.threshold_min ?? "") || max !== String(rule.threshold_max ?? "");
  useEffect(() => {
    const id = `dfm-rule:${rule.id}`;
    useDraftGuardStore.getState().register(id, dirty ? { label: "DFM 阈值草稿", discard: () => setDraft({}) } : null);
    return () => useDraftGuardStore.getState().register(id, null);
  }, [dirty, rule.id, rule.threshold_min, rule.threshold_max]);
  const update = async (body: Record<string, number | boolean>) => {
    if (submitting.current || uncertainUpdate) return;
    submitting.current = true; setPending(true); setError("");
    setUncertainUpdate(body);
    let failure = '';
    try {
      const saved = await readJson<DFMRuleData>(await authFetch(`${API_BASE}/api/dfm/rules/${encodeURIComponent(rule.id)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      }), "规则保存失败");
      if (!saved || saved.id !== rule.id) throw new Error("规则保存结果无效");
    } catch (reason) { failure = reason instanceof Error ? reason.message : "规则保存失败"; }
    try { await reconcile(body, failure); }
    finally { submitting.current = false; setPending(false); }
  };
  const reconcile = async (body: Record<string, number | boolean>, failure = '') => {
    setPending(true);
    try {
      const sets = await readJson<RuleSet[]>(await authFetch(`${API_BASE}/api/dfm/rules`), '规则结果核对失败');
      if (!Array.isArray(sets)) throw new Error('规则列表格式无效');
      const actual = sets.flatMap(set => set.rules).find(item => item.id === rule.id && item.process === rule.process);
      if (!actual) throw new Error('未找到待核对规则');
      const applied = Object.entries(body).every(([key, value]) => actual[key as keyof DFMRuleData] === value);
      setUncertainUpdate(null);
      onSaved(actual);
      if (applied) {
        // Only acknowledge fields that still contain this submission's value.
        // In particular, an enabled-only write cannot clear threshold edits.
        setDraft(current => {
          const next = { ...current };
          for (const field of ['threshold_min', 'threshold_max'] as const) {
            if (field in body && next[field]?.trim() && Number(next[field]) === body[field]) delete next[field];
          }
          return next;
        });
        setError('');
      }
      else setError(`${failure ? failure + '；' : ''}已核对服务器，修改尚未生效，草稿已保留。`);
    } catch { setError('操作结果未确认，请恢复连接后核对结果。'); }
    finally { setPending(false); }
  };
  const save = () => {
    const values: Record<string, number> = {};
    if (rule.threshold_min !== null) values.threshold_min = min.trim() ? Number(min) : NaN;
    if (rule.threshold_max !== null) values.threshold_max = max.trim() ? Number(max) : NaN;
    if (Object.values(values).some(value => !Number.isFinite(value))) { setError("请输入有效阈值"); return; }
    if (values.threshold_min !== undefined && values.threshold_max !== undefined && values.threshold_min > values.threshold_max) { setError("最小阈值不能大于最大阈值"); return; }
    void update(values);
  };
  return <article className={`ww-rule ${rule.enabled ? "" : "ww-rule--inactive"}`} aria-busy={pending}>
    <label className="ww-rule-label">
      <input type="checkbox" checked={rule.enabled} disabled={pending || Boolean(uncertainUpdate)} onChange={event => void update({ enabled: event.target.checked })} />
      <span className="ww-rule-description"><span className="block text-[var(--ink)]">{rule.description}</span>
        <span className="block type-caption text-[var(--muted)]">{rule.check_type === "geometric" ? "几何规则" : "确定性启发规则"} · {rule.severity} · {rule.enabled ? "已启用" : "未启用"}</span>
      </span>
    </label>
    {rule.check_type === "geometric" && (rule.threshold_min !== null || rule.threshold_max !== null) ? <div className="ww-rule-thresholds">
      {rule.threshold_min !== null ? <label>最小值 ({rule.unit})<input aria-invalid={Boolean(error)} disabled={pending} type="number" step="any" value={min} onChange={event => edit('threshold_min', event.target.value)} /></label> : null}
      {rule.threshold_max !== null ? <label>最大值 ({rule.unit})<input aria-invalid={Boolean(error)} disabled={pending} type="number" step="any" value={max} onChange={event => edit('threshold_max', event.target.value)} /></label> : null}
      <button className="workspace-button" type="button" disabled={!dirty || pending || Boolean(uncertainUpdate)} onClick={save}>{pending ? "正在保存" : "保存阈值"}</button>
    </div> : null}
    {error ? <p role="alert" className="mt-2 type-caption text-red-700">{error}</p> : null}
    {uncertainUpdate ? <div role="status"><p>操作结果未确认；可继续编辑草稿，核对后才能保存。</p><button className="workspace-button" type="button" disabled={pending} onClick={() => void reconcile(uncertainUpdate)}>核对结果</button></div> : null}
  </article>;
}

export default function DFMRuleConfig({ embedded = false }: { embedded?: boolean }) {
  const [open, setOpen] = useState(embedded);
  const [ruleSets, setRuleSets] = useState<RuleSet[]>([]);
  const [activeProcess, setActiveProcess] = useState("");
  const [loading, setLoading] = useState(embedded);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void authFetch(`${API_BASE}/api/dfm/rules`, { signal: controller.signal }).then(response => readJson<RuleSet[]>(response, "规则加载失败")).then(data => {
      if (!Array.isArray(data)) throw new Error("规则列表格式无效");
      if (controller.signal.aborted) return;
      setRuleSets(data); setActiveProcess(previous => data.some(item => item.process === previous) ? previous : data[0]?.process || "");
    }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "规则加载失败"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, attempt]);
  if (!open) return <button className="ww-settings-entry" onClick={() => { setLoading(true); setError(""); setOpen(true); }} type="button">DFM 规则配置</button>;
  const active = ruleSets.find(item => item.process === activeProcess);
  return <section className="space-y-3 p-4" aria-busy={loading}>
    <header className="flex items-center justify-between gap-2"><h4 className="type-section-heading">DFM 规则配置</h4>
      {!embedded ? <button className="workspace-button" type="button" onClick={() => guardDraft(() => setOpen(false))}>收起</button> : null}</header>
    {loading ? <p role="status">加载中...</p> : null}
    {error ? <div role="alert"><p className="text-red-700">{error}{ruleSets.length ? "；以下为上次读取的数据。" : ""}</p><button className="workspace-button mt-2" type="button" disabled={loading} onClick={() => { setLoading(true); setError(""); setAttempt(value => value + 1); }}>重试</button></div> : null}
    {!loading && !error && !ruleSets.length ? <p>暂无规则集</p> : null}
    <div className="flex flex-wrap gap-2">{[...new Set(ruleSets.map(item => item.process))].map(process => <button className="workspace-button" aria-pressed={activeProcess === process} key={process} type="button" onClick={() => guardDraft(() => setActiveProcess(process))}>{PROCESS_LABELS[process] || process}</button>)}</div>
    <div className="ww-rule-list">{active?.rules.map(rule => <RuleRow key={`${rule.process}:${rule.id}`} rule={rule} onSaved={saved => setRuleSets(previous => previous.map(set => ({ ...set, rules: set.rules.map(item => item.id === saved.id ? saved : item) })))} />)}</div>
    {!loading && !error && active && !active.rules.length ? <p>此工艺暂无规则</p> : null}
  </section>;
}
