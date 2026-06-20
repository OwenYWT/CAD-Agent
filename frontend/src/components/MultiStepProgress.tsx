interface StepInfo {
  phase: string;
  description: string;
  status: "pending" | "running" | "done" | "error";
}

interface MultiStepProgressProps {
  steps: StepInfo[] | null;
}

function StatusIcon({ status }: { status: StepInfo["status"] }) {
  switch (status) {
    case "done":
      return <span className="text-green-500">&#10003;</span>;
    case "running":
      return (
        <span className="inline-block w-3 h-3 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
      );
    case "error":
      return <span className="text-red-500">&#10007;</span>;
    default:
      return <span className="text-gray-300">&#9675;</span>;
  }
}

export default function MultiStepProgress({ steps }: MultiStepProgressProps) {
  if (!steps || steps.length === 0) return null;

  return (
    <div className="bg-white border-t border-gray-200 p-3">
      <h3 className="text-sm font-medium text-gray-700 mb-2">
        多步构建 ({steps.filter((s) => s.status === "done").length}/{steps.length})
      </h3>
      <div className="space-y-1">
        {steps.map((step, i) => (
          <div key={i} className="flex items-center gap-2 text-xs">
            <StatusIcon status={step.status} />
            <span className="text-gray-500 w-16 shrink-0">[{step.phase}]</span>
            <span
              className={`flex-1 ${
                step.status === "running"
                  ? "text-indigo-700 font-medium"
                  : step.status === "error"
                  ? "text-red-600"
                  : "text-gray-600"
              }`}
            >
              {step.description}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
