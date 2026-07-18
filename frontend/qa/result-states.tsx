import { useState } from "react";
import { createRoot } from "react-dom/client";
import "../src/index.css";
import { ResultCard } from "../src/components/ChatPanel";
import ParameterPanel from "../src/components/ParameterPanel";
import Viewer3D from "../src/components/Viewer3D";
import type { GenerationResult, ParamConfig } from "../src/types";

const success: GenerationResult = {
  request_id: "contract-success",
  success: true,
  files: { step: "/api/files/contract/model.step", stl: "/api/files/contract/model.stl" },
  code: "width = 60\nheight = 40\nresult = box(width, height)",
  params: { width: { value: 60, comment: "宽度" }, height: { value: 40, comment: "高度" } },
  validation: {
    is_watertight: true,
    bounding_box: { x_min: 0, x_max: 60, y_min: 0, y_max: 40, z_min: 0, z_max: 20 },
    volume: 48000,
    printable: true,
    fits_build_volume: true,
    min_wall_thickness: 1.6,
  },
};

const warning: GenerationResult = {
  ...success,
  request_id: "contract-warning",
  validation: {
    ...success.validation!,
    printable: false,
    is_watertight: false,
    print_warnings: ["模型存在开放边，切片前需要修复。"],
  },
};

const failure: GenerationResult = {
  request_id: "contract-failure",
  success: false,
  error: { type: "GenerationError", message: "没有生成有效几何体，请补充尺寸后重试。" },
};

export function ParameterBoundaryCheck() {
  const [baseline, setBaseline] = useState("A");
  const params: Record<string, ParamConfig> = baseline === "A"
    ? { width: { value: 60, comment: "宽度" } }
    : { width: { value: 100, comment: "宽度" } };
  const code = baseline === "A" ? "width = 60" : "width = 100";
  return (
    <section className="rounded-md border border-slate-200 bg-white">
      <div className="flex items-center justify-between border-b border-slate-100 p-3">
        <h2 className="text-sm font-semibold">参数基线检查：{baseline}</h2>
        <button className="min-h-10 rounded-md bg-slate-900 px-3 text-xs text-white" onClick={() => setBaseline((value) => value === "A" ? "B" : "A")} type="button">切换基线</button>
      </div>
      <ParameterPanel baselineKey={`qa:${baseline}`} code={code} key={baseline} onCodeChange={() => true} params={params} validation={success.validation} />
    </section>
  );
}

export function QAApp() {
  const [retried, setRetried] = useState(false);
  return (
    <main className="min-h-screen bg-slate-100 p-4 sm:p-6">
      <div className="mx-auto max-w-6xl space-y-5">
        <header><h1 className="text-xl font-semibold text-slate-900">生产组件状态检查</h1><p className="text-sm text-slate-500">此页面仅用于开发验收，不进入正式应用入口。</p></header>
        <div className="grid gap-4 lg:grid-cols-3">
          <ResultCard onSwitchTab={() => {}} onViewModel={() => {}} result={success} />
          <ResultCard onSwitchTab={() => {}} onViewModel={() => {}} result={warning} />
          <div><ResultCard onRetry={() => setRetried(true)} result={failure} />{retried && <p className="mt-2 text-xs text-sky-700" role="status">重试描述已恢复</p>}</div>
        </div>
        <ParameterBoundaryCheck />
        <section className="h-80 overflow-hidden rounded-md border border-slate-800"><Viewer3D stlUrl="/qa/missing-model.stl" /></section>
      </div>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<QAApp />);
