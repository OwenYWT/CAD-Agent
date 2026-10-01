import { useState } from "react";
import { changeDocumentMember, getDocumentMembers } from "../../services/clients/documents";
import type { ProjectMember } from "../../services/clients/documents";

export default function ProjectMembers({ documentId }: { documentId: string }) {
  const [members, setMembers] = useState<ProjectMember[] | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    setPending(true); setError("");
    try { setMembers(await getDocumentMembers(documentId)); }
    catch (e) { setError(e instanceof Error ? e.message : "成员列表加载失败"); }
    finally { setPending(false); }
  };
  const change = async (member: ProjectMember, role: "viewer" | "editor" | null) => {
    setPending(true); setError("");
    try {
      await changeDocumentMember(documentId, member, role);
      setMembers(await getDocumentMembers(documentId));
    } catch (e) { setError(e instanceof Error ? e.message : "成员权限更新失败"); }
    finally { setPending(false); }
  };
  return <div className="ww-inspector-section">
    <button type="button" className="workspace-button" disabled={pending} onClick={() => void load()}>管理项目成员</button>
    {members ? <p className="mt-2 type-caption">权限适用于本项目所有文档。移除成员会撤销访问权限并释放其编辑租约。</p> : null}
    {members?.map((member) => <div key={member.principal_id} data-testid={`member-${member.principal_id}`} className="my-3 type-caption">
      <p>{member.display_name} · {member.principal_id.slice(0, 8)}</p>
      {member.role === "viewer" || member.role === "editor" ? <div className="mt-1 flex gap-2">
        <select aria-label={`成员 ${member.principal_id.slice(0, 8)} 权限`} className="rounded border border-[var(--line)] p-2"
          disabled={pending} value={member.role} onChange={(e) => void change(member, e.target.value as "viewer" | "editor")}>
          <option value="viewer">审阅者</option><option value="editor">编辑者</option>
        </select>
        <button type="button" className="workspace-button" disabled={pending} onClick={() => void change(member, null)}>移除成员</button>
      </div> : <span>{member.role === "owner" ? "项目所有者" : "管理员"}</span>}
    </div>)}
    {error ? <p role="alert" className="mt-2 type-caption text-red-700">{error}</p> : null}
  </div>;
}
