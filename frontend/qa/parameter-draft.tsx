import { useState } from "react";
import { createRoot } from "react-dom/client";
import ParameterDrawer from "../src/components/parameters/ParameterDrawer";
import type { Parameter } from "../src/types/engineering";
import "../src/index.css";

// Component state fixture only. This QA entry cannot submit a modeling task.
const unavailable = (): never => { throw new Error("组件验收页不连接建模服务"); };
function ParameterDraftQA() {
  const [parameters, setParameters] = useState<Parameter[]>([]);
  const [open, setOpen] = useState(false);
  const [readOnly, setReadOnly] = useState(false);
  return <main className="h-screen max-w-md">
    <p>参数编辑器组件验收</p>
    <button onClick={() => setParameters([{ id: "diameter", label: "孔径", value: 6.5, defaultValue: 6.5, unit: "mm", group: "基础尺寸", comment: "组件测试尺寸" }])}>加载参数</button>
    <button onClick={() => setOpen(true)}>打开属性</button>
    <label><input type="checkbox" checked={readOnly} onChange={e => setReadOnly(e.target.checked)} />只读</label>
    <ParameterDrawer embedded open={open} parameters={parameters} result={null} isGenerating={false} canEdit={!readOnly}
      onClose={() => setOpen(false)} onExecute={unavailable} onModifyParameters={unavailable} />
  </main>;
}
createRoot(document.getElementById("root")!).render(<ParameterDraftQA />);
