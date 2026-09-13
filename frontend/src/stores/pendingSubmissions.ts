// A sent request is retained until its matching server acknowledgement. Reconnect
// replays exactly these bytes and the same key, never a freshly rebased request.
type Submission = Record<string, unknown> & { idempotency_key: string };
const key = (owner: string | null, session: string, panel: string) => `cad-submission:${owner}:${session}:${panel}`;

export function pendingSubmission(owner: string | null, session: string, panel: string): Submission | null {
  try {
    const raw = sessionStorage.getItem(key(owner, session, panel));
    if (!raw) return null;
    const value = JSON.parse(raw) as Submission;
    return typeof value.idempotency_key === "string" && value.panel_id === panel ? value : null;
  } catch { return null; }
}

export function rememberSubmission(owner: string | null, session: string, panel: string, request: Submission): boolean {
  if (pendingSubmission(owner, session, panel)) return false;
  try { sessionStorage.setItem(key(owner, session, panel), JSON.stringify(request)); return true; }
  catch { return false; }
}

export function acknowledgeSubmission(owner: string | null, session: string, panel: string, id?: string) {
  const request = pendingSubmission(owner, session, panel);
  if (id && request?.idempotency_key === id) sessionStorage.removeItem(key(owner, session, panel));
}
