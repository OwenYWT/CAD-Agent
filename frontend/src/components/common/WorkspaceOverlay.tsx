import { useEffect, type ReactNode } from "react";
import { Icon } from "../ui/Icon";

interface OverlayProps {
  open: boolean;
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}

function useEscape(open: boolean, onClose: () => void) {
  useEffect(() => {
    if (!open) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);
}

export function WorkspaceDrawer({ open, title, description, onClose, children, footer }: OverlayProps) {
  useEscape(open, onClose);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[70]" role="presentation">
      <button aria-label={`关闭${title}`} className="absolute inset-0 bg-slate-950/20 backdrop-blur-[1px]" onClick={onClose} type="button" />
      <aside aria-label={title} aria-modal="true" className="absolute inset-y-0 right-0 flex w-full max-w-[440px] flex-col border-l border-[var(--line)] bg-white shadow-2xl" role="dialog">
        <header className="flex min-h-[64px] items-start gap-3 border-b border-[var(--line)] px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold text-[var(--ink)]">{title}</h2>
            {description ? <p className="mt-1 text-xs leading-5 text-[var(--muted)]">{description}</p> : null}
          </div>
          <button aria-label="关闭" className="workspace-icon-button" onClick={onClose} type="button"><Icon name="x" size={17} /></button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer ? <footer className="border-t border-[var(--line)] bg-white px-5 py-3">{footer}</footer> : null}
      </aside>
    </div>
  );
}

export function WorkspaceDialog({ open, title, description, onClose, children, footer }: OverlayProps) {
  useEscape(open, onClose);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[75] flex items-center justify-center p-4 sm:p-8">
      <button aria-label={`关闭${title}`} className="absolute inset-0 bg-slate-950/25 backdrop-blur-[1px]" onClick={onClose} type="button" />
      <section aria-label={title} aria-modal="true" className="relative flex max-h-[88dvh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-[var(--line)] bg-white shadow-2xl" role="dialog">
        <header className="flex min-h-[64px] items-start gap-3 border-b border-[var(--line)] px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold text-[var(--ink)]">{title}</h2>
            {description ? <p className="mt-1 text-xs leading-5 text-[var(--muted)]">{description}</p> : null}
          </div>
          <button aria-label="关闭" className="workspace-icon-button" onClick={onClose} type="button"><Icon name="x" size={17} /></button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer ? <footer className="border-t border-[var(--line)] bg-white px-5 py-3">{footer}</footer> : null}
      </section>
    </div>
  );
}

export function InlineState({ title, detail, actionLabel, onAction, tone = "neutral" }: { title: string; detail: string; actionLabel?: string; onAction?: () => void; tone?: "neutral" | "error" | "info" }) {
  const toneClass = tone === "error" ? "border-red-200 bg-red-50 text-red-800" : tone === "info" ? "border-sky-200 bg-sky-50 text-sky-900" : "border-[var(--line)] bg-[var(--subtle)] text-[var(--ink)]";
  return (
    <div className={`rounded-lg border p-4 ${toneClass}`} role={tone === "error" ? "alert" : "status"}>
      <p className="text-sm font-medium">{title}</p>
      <p className="mt-1 text-xs leading-5 opacity-75">{detail}</p>
      {actionLabel && onAction ? <button className="workspace-button mt-3" onClick={onAction} type="button">{actionLabel}</button> : null}
    </div>
  );
}
