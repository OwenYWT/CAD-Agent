import type { CloudDocument, DocumentOperation } from '../types/document';

export function parameterSubmissionState(id: string | null, operations: DocumentOperation[], document: CloudDocument) {
  const operation = id ? operations.find(item => item.id === id) : undefined;
  const saved = operation?.status === 'committed' && !!operation.result_revision_id
    && document.head_revision_id === operation.result_revision_id && document.revision_id === operation.result_revision_id
    && (!document.view_mode || document.view_mode === 'committed');
  const failed = !!operation && ['failed','cancelled','rejected','rolled_back','changes_requested'].includes(operation.status);
  return {operation, saved, failed};
}
