import { useEffect, useEffectEvent, type RefObject } from "react";

const stack: HTMLElement[] = [];
let rootBeforeInert: boolean | undefined;
export function useModalFocus(open: boolean, ref: RefObject<HTMLElement | null>, onClose: () => void) {
  const close = useEffectEvent(onClose);
  useEffect(() => {
    const modal = ref.current;
    if (!open || !modal) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const root = document.getElementById("root");
    if (!stack.length) rootBeforeInert = root?.inert;
    const underlying = stack.at(-1);
    const underlyingInert = underlying?.inert;
    if (underlying) underlying.inert = true;
    if (root && !root.contains(modal)) root.inert = true;
    stack.push(modal);
    const elements = () => Array.from(modal.querySelectorAll<HTMLElement>(
      'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])',
    )).filter(el => !el.closest('[hidden], [inert], [aria-hidden="true"]') && el.getClientRects().length > 0);
    (modal.querySelector<HTMLElement>('[autofocus]') || elements()[0] || modal).focus();
    const key = (event: KeyboardEvent) => {
      if (stack.at(-1) !== modal) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); close(); }
      if (event.key !== "Tab") return;
      const items = elements(), first = items[0], last = items.at(-1);
      if (!first || !last) { event.preventDefault(); modal.focus(); return; }
      if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    const focus = (event: FocusEvent) => {
      if (stack.at(-1) === modal && !modal.contains(event.target as Node)) (elements()[0] || modal).focus();
    };
    document.addEventListener("keydown", key, true);
    document.addEventListener("focusin", focus);
    return () => {
      stack.splice(stack.indexOf(modal), 1);
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("focusin", focus);
      if (root && !stack.length && rootBeforeInert !== undefined) {root.inert = rootBeforeInert;rootBeforeInert=undefined;}
      if (underlying && underlyingInert !== undefined) underlying.inert = underlyingInert;
      if (previous?.isConnected && !previous.closest("[inert]")) previous.focus();
    };
  }, [open, ref]);
}
