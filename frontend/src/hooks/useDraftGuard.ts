import { useEffect, useId } from "react";
import { useDraftGuardStore } from "../stores/draftGuard";

export function useDraftGuard(dirty: boolean, label: string, discard: () => void) {
  const id = useId();
  useEffect(() => {
    useDraftGuardStore.getState().register(id, dirty ? { label, discard } : null);
    return () => useDraftGuardStore.getState().register(id, null);
  }, [id, dirty, label, discard]);
}
