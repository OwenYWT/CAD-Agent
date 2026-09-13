import type { CloudDocument } from "../../types/document";
import type { RequirementBasis } from "../../types/requirements";
import type { ConnectionState } from "../../hooks/useWebSocket";
import type { EngineeringDomain } from "../../types/engineering";
import { WorkspaceDrawer } from "../common/WorkspaceOverlay";
import AgentPanel from "./AgentPanel";
import { AGENT_CONTEXT_LABELS } from "./agentContext";

interface AgentDrawerProps {
  open: boolean;
  context: EngineeringDomain;
  connection: ConnectionState;
  suggestedPrompt?: string;
  onClose: () => void;
  onSend: (text: string, basis?: RequirementBasis) => boolean;
  onCancel?: () => void;
  selectionLabel?: string;
  onClearSelection?: () => void;
  blockedReason?: string;
  document?:CloudDocument | null;isAdmin?:boolean;canModify?:boolean;onRetry?:()=>Promise<void>;onRecover?:(target:string)=>void;onReview?:()=>void;
}

export default function AgentDrawer({ open, context, connection, suggestedPrompt, onClose, onSend, onCancel, selectionLabel, onClearSelection, blockedReason,document,isAdmin,canModify,onRetry,onRecover,onReview }: AgentDrawerProps) {
  return (
    <WorkspaceDrawer
      description={`当前上下文：${AGENT_CONTEXT_LABELS[context]}。请求确认后通过现有 WebSocket 提交。`}
      onClose={onClose}
      open={open}
      title="询问 Agent"
    >
      <AgentPanel document={document} isAdmin={isAdmin} canModify={canModify} onRetry={onRetry} onRecover={onRecover} onReview={onReview} connection={connection} context={context} onCancel={onCancel} onSend={onSend} suggestedPrompt={suggestedPrompt}
        selectionLabel={selectionLabel} onClearSelection={onClearSelection} blockedReason={blockedReason} />
    </WorkspaceDrawer>
  );
}
