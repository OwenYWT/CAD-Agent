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
  onSend: (text: string) => boolean;
  onCancel?: () => void;
}

export default function AgentDrawer({ open, context, connection, suggestedPrompt, onClose, onSend, onCancel }: AgentDrawerProps) {
  return (
    <WorkspaceDrawer
      description={`当前上下文：${AGENT_CONTEXT_LABELS[context]}。请求确认后通过现有 WebSocket 提交。`}
      onClose={onClose}
      open={open}
      title="询问 Agent"
    >
      <AgentPanel connection={connection} context={context} onCancel={onCancel} onSend={onSend} suggestedPrompt={suggestedPrompt} />
    </WorkspaceDrawer>
  );
}
