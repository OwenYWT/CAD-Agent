import { create } from "zustand";

interface Draft { label: string; discard: () => void }
interface DraftGuard {
  drafts: Record<string, Draft>;
  pending: (() => void) | null;
  register: (id: string, draft: Draft | null) => void;
  request: (transition: () => void) => boolean;
  stay: () => void;
  discardAndContinue: () => void;
}

export const useDraftGuardStore = create<DraftGuard>((set, get) => ({
  drafts: {}, pending: null,
  register: (id, draft) => set(state => {
    const drafts = { ...state.drafts };
    if (draft) drafts[id] = draft; else delete drafts[id];
    return { drafts };
  }),
  request: transition => {
    if (Object.keys(get().drafts).length) { set({ pending: transition }); return false; }
    transition(); return true;
  },
  stay: () => set({ pending: null }),
  discardAndContinue: () => {
    const { drafts, pending } = get();
    set({ drafts: {}, pending: null });
    Object.values(drafts).forEach(draft => draft.discard());
    pending?.();
  },
}));

export const guardDraft = (transition: () => void) => useDraftGuardStore.getState().request(transition);
