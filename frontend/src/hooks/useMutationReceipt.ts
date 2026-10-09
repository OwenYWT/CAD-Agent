import { useState } from 'react';
import { getAuthUser } from '../auth';

/** Retain an uncertain mutation's identity across panel close/reopen. */
export function useMutationReceipt<T extends object>(scope: string) {
  const workspace = new URLSearchParams(window.location.search).get('workspace') || 'own';
  const key = `cad-mutation:${getAuthUser()?.id || 'local'}:${workspace}:${scope}`;
  const read = (): T | null => {
    try {
      const value = JSON.parse(sessionStorage.getItem(key) || 'null');
      return value && typeof value === 'object' && !Array.isArray(value) ? value as T : null;
    } catch { return null; }
  };
  const [receipt, setReceipt] = useState(() => ({ key, value: read() }));
  const value = receipt.key === key ? receipt.value : read();
  const save = (next: T | null) => {
    if (next) sessionStorage.setItem(key, JSON.stringify(next));
    else sessionStorage.removeItem(key);
    setReceipt({ key, value: next });
  };
  return [value, save] as const;
}
