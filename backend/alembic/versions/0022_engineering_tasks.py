"""Immutable native engineering task provenance, separate from CAD mutations."""
from alembic import op

revision='0022_engineering_tasks'
down_revision='0021_document_branches'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("""CREATE TABLE document_engineering_tasks(
        workflow_run_id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,document_id uuid NOT NULL,
        source_revision_id uuid NOT NULL,source_state_version bigint NOT NULL,source_artifact_id uuid NOT NULL,
        source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
        task_kind text NOT NULL,principal_id uuid NOT NULL,idempotency_key text NOT NULL,request_hash text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tenant_id,principal_id,idempotency_key),
        FOREIGN KEY(tenant_id,workflow_run_id) REFERENCES workflow_runs(tenant_id,id),
        FOREIGN KEY(tenant_id,project_id,document_id,source_revision_id) REFERENCES project_revisions(tenant_id,project_id,branch_id,id),
        FOREIGN KEY(tenant_id,principal_id) REFERENCES principals(tenant_id,id),
        FOREIGN KEY(tenant_id,source_artifact_id) REFERENCES artifacts(tenant_id,id)
    )""")
    op.execute('ALTER TABLE document_engineering_tasks ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE document_engineering_tasks FORCE ROW LEVEL SECURITY')
    op.execute("""CREATE POLICY document_engineering_tasks_tenant ON document_engineering_tasks
        USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
        WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
    op.execute('GRANT SELECT,INSERT ON document_engineering_tasks TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator')
    op.execute('CREATE TRIGGER document_engineering_tasks_immutable BEFORE UPDATE OR DELETE ON document_engineering_tasks FOR EACH ROW EXECUTE FUNCTION reject_document_evidence_mutation()')


def downgrade():
    op.execute('DROP TABLE document_engineering_tasks')
