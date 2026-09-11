import type { CloudDocument } from "../types/document";

// Observed server heads are separate from candidate/task snapshots.
const heads = new Map<string, CloudDocument>();
export const observeDocument = (document: CloudDocument) => heads.set(document.document_id, document);
export const observedDocument = (id: string) => heads.get(id);
export const forgetDocument = (id: string) => heads.delete(id);
