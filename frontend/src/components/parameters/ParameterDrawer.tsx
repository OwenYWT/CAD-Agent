import { useMemo, useState } from "react";
import { patchCodeParameters } from "../../adapters/projectAdapter";
import type { GenerationResult } from "../../types";
import type { Parameter } from "../../types/engineering";
import { parameterValueError } from "../../utils/parameterValidation";
import { WorkspaceDrawer } from "../common/WorkspaceOverlay";

const GROUPS: Parameter["group"][] = ["基础尺寸", "材料", "装配参数", "制造参数", "高级参数"];

interface ParameterDrawerProps {
  open: boolean;
  parameters: Parameter[];
  result: GenerationResult | null;
  isGenerating: boolean;
  onClose: () => void;
  onExecute: (code: string) => boolean;
  onModifyParameters: (
    updates: { parameter_id: string; value: number }[],
  ) => boolean;
}

export default function ParameterDrawer({ open, parameters, result, isGenerating, onClose, onExecute, onModifyParameters }: ParameterDrawerProps) {
  const baseline = useMemo(() => Object.fromEntries(parameters.map((parameter) => [parameter.id, parameter.value])), [parameters]);
  const [draft, setDraft] = useState<Record<string, number>>(baseline);
  const [error, setError] = useState<string | null>(null);

  const changed = parameters.filter((parameter) => draft[parameter.id] !== parameter.value);
  const invalid = changed.filter((parameter) => parameterValueError(parameter, draft[parameter.id]));
  const save = () => {
    if (invalid.length) { setError("请先修正无效参数。"); return; }
    const sourceParameters = new Map(
      (result?.parameters || []).map((parameter) => [parameter.name, parameter]),
    );
    const freecadEdit = changed.every(
      (parameter) => sourceParameters.get(parameter.id)?.source === "freecad",
    );
    if (freecadEdit) {
      if (!result?.parameter_state_sha256) {
        setError("当前 FreeCAD 参数状态缺少完整性标识，无法安全修改。");
        return;
      }
      if (!onModifyParameters(changed.map((parameter) => ({
        parameter_id: parameter.id,
        value: draft[parameter.id],
      })))) {
        setError("实时连接尚未就绪，参数草稿已保留。");
        return;
      }
      setError(null);
      onClose();
      return;
    }
    if (!result?.code) { setError("当前结果没有可修改的参数化代码。"); return; }
    const values = Object.fromEntries(changed.map((parameter) => [parameter.id, draft[parameter.id]]));
    const nextCode = patchCodeParameters(result.code, values);
    if (nextCode === result.code) { setError("参数未能映射到当前代码，未执行修改。"); return; }
    if (!onExecute(nextCode)) { setError("实时连接尚未就绪，参数草稿已保留。"); return; }
    setError(null);
    onClose();
  };

  return (
    <WorkspaceDrawer description="修改会先保留为草稿，点击保存后基于当前工程版本重新计算模型。执行失败时保留最后一次成功模型。" footer={<div className="flex items-center justify-between gap-2"><button className="workspace-button" disabled={!changed.length} onClick={() => { setDraft(baseline); setError(null); }} type="button">恢复默认值</button><div className="flex gap-2"><button className="workspace-button" onClick={onClose} type="button">取消</button><button className="workspace-button workspace-button--primary" disabled={!changed.length || invalid.length > 0 || isGenerating} onClick={save} type="button">{isGenerating ? "重新计算中" : "保存参数"}</button></div></div>} onClose={onClose} open={open} title="机械属性">
      <div className="space-y-5 p-5">
        {error ? <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 type-body text-red-800" role="alert">{error}</div> : null}
        {parameters.length === 0 ? <div className="rounded-lg border border-[var(--line)] bg-[var(--subtle)] p-4 type-body text-[var(--muted)]">当前结果没有可编辑参数。生成参数化模型后，这里会显示真实参数。</div> : GROUPS.map((group) => {
          const items = parameters.filter((parameter) => parameter.group === group);
          if (!items.length) return null;
          return <section key={group}><h3 className="mb-2 type-section-heading  uppercase tracking-[0.08em] text-[var(--faint)]">{group}</h3><div className="overflow-hidden rounded-lg border border-[var(--line)]">{items.map((parameter) => {
            const draftValue = draft[parameter.id];
            const valueError = parameterValueError(parameter, draftValue);
            return <label className="grid min-h-[58px] grid-cols-[minmax(0,1fr)_132px] items-center gap-3 border-b border-[var(--line)] px-3 py-2 last:border-0" key={parameter.id}><span className="min-w-0"><span className="block type-body  text-[var(--ink)]">{parameter.label}</span><span className="mt-0.5 block truncate type-caption text-[var(--faint)]">原值 {parameter.value}{parameter.unit} · {parameter.id}</span></span><span><span className="relative block"><input aria-invalid={Boolean(valueError)} className={`h-9 w-full rounded-md border bg-white px-2 pr-8 text-right type-control tabular-nums outline-none focus:border-[var(--focus)] ${valueError ? "border-red-300" : "border-[var(--line-strong)]"}`} max={parameter.max} min={parameter.min} onChange={(event) => { setDraft((values) => ({ ...values, [parameter.id]: event.target.valueAsNumber })); setError(null); }} step={parameter.step ?? "any"} type="number" value={Number.isFinite(draftValue) ? draftValue : ""} />{parameter.unit ? <span className="pointer-events-none absolute right-2 top-2.5 type-caption text-[var(--faint)]">{parameter.unit}</span> : null}</span>{valueError ? <span className="mt-1 block type-caption text-red-600">{valueError}</span> : null}</span></label>;
          })}</div></section>;
        })}
      </div>
    </WorkspaceDrawer>
  );
}
