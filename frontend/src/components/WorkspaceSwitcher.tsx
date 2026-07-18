import { Icon } from "./ui/Icon";

export type MobileWorkspace = "model" | "preview" | "result";

interface WorkspaceSwitcherProps {
  active: MobileWorkspace;
  hasResult: boolean;
  onChange: (workspace: MobileWorkspace) => void;
}

const items = [
  { key: "model" as const, label: "建模", icon: "message" as const },
  { key: "preview" as const, label: "预览", icon: "box" as const },
  { key: "result" as const, label: "结果", icon: "sliders" as const },
];

export default function WorkspaceSwitcher({ active, hasResult, onChange }: WorkspaceSwitcherProps) {
  return (
    <nav aria-label="移动端工作区" className="workspace-switcher md:hidden">
      {items.map((item) => {
        const selected = active === item.key;
        const ready = hasResult && item.key !== "model";
        return (
          <button
            aria-current={selected ? "page" : undefined}
            className="workspace-switcher__item"
            data-active={selected || undefined}
            key={item.key}
            onClick={() => onChange(item.key)}
            type="button"
          >
            <span className="relative">
              <Icon name={item.icon} size={17} />
              {ready && <span aria-label="已有结果" className="workspace-switcher__dot" />}
            </span>
            <span>{item.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
