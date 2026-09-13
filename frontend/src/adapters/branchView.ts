import type { BranchComparison, DocumentBranches } from "../types/branches";
import type { CloudDocument } from "../types/document";

export function comparisonStaleness(comparison: BranchComparison, target: CloudDocument, branches: DocumentBranches | null) {
  const source = branches?.branches.find(branch => branch.document_id === comparison.source_document_id);
  return {
    target: comparison.document_id !== target.document_id || comparison.target_revision_id !== target.head_revision_id
      || comparison.target_state_version !== target.state_version,
    source: !source || source.head_revision_id !== comparison.source_revision_id || source.state_version !== comparison.source_state_version,
    sourceKnown: !!source,
  };
}
