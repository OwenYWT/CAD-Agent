"""Immutable named releases sharing the frozen engineering execution source."""
from alembic import op

revision='0023_document_releases'
down_revision='0022_engineering_tasks'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE document_engineering_tasks ADD CONSTRAINT uq_engineering_task_document_workflow UNIQUE(tenant_id,document_id,workflow_run_id)')
    op.execute('''CREATE TABLE document_releases(
        workflow_run_id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL,document_id uuid NOT NULL,release_name text NOT NULL CHECK(length(release_name) BETWEEN 1 AND 120),
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tenant_id,document_id,release_name),
        FOREIGN KEY(tenant_id,document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,document_id,workflow_run_id) REFERENCES document_engineering_tasks(tenant_id,document_id,workflow_run_id),
        FOREIGN KEY(tenant_id,workflow_run_id) REFERENCES workflow_runs(tenant_id,id)
    )''')
    op.execute('ALTER TABLE document_releases ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE document_releases FORCE ROW LEVEL SECURITY')
    op.execute("""CREATE POLICY document_releases_tenant ON document_releases
        USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
        WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
    op.execute('GRANT SELECT,INSERT ON document_releases TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator')
    op.execute('CREATE TRIGGER document_releases_immutable BEFORE UPDATE OR DELETE ON document_releases FOR EACH ROW EXECUTE FUNCTION reject_document_evidence_mutation()')
    op.execute('CREATE INDEX document_engineering_tasks_document_created ON document_engineering_tasks(document_id,created_at DESC)')
    op.execute('CREATE INDEX document_engineering_tasks_revision_kind ON document_engineering_tasks(document_id,source_revision_id,task_kind)')


def downgrade():
    op.execute('DROP INDEX document_engineering_tasks_revision_kind')
    op.execute('DROP INDEX document_engineering_tasks_document_created')
    op.execute('DROP TABLE document_releases')
    op.execute('ALTER TABLE document_engineering_tasks DROP CONSTRAINT uq_engineering_task_document_workflow')
