"""Immutable fork lineage and reviewed merge provenance."""
from alembic import op

revision = '0021_document_branches'
down_revision = '0020_document_geometry'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE document_forks (
        document_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL,
        source_document_id uuid NOT NULL, source_revision_id uuid NOT NULL, source_state_version bigint NOT NULL,
        seed_revision_id uuid NOT NULL, lineage_id uuid NOT NULL, principal_id uuid NOT NULL,
        idempotency_key text NOT NULL, request_hash text NOT NULL, workflow_run_id uuid,
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tenant_id,principal_id,idempotency_key),
        FOREIGN KEY(tenant_id,document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,project_id,document_id) REFERENCES project_branches(tenant_id,project_id,id),
        FOREIGN KEY(tenant_id,project_id,source_document_id,source_revision_id) REFERENCES project_revisions(tenant_id,project_id,branch_id,id),
        FOREIGN KEY(tenant_id,project_id,document_id,seed_revision_id) REFERENCES project_revisions(tenant_id,project_id,branch_id,id),
        FOREIGN KEY(tenant_id,source_document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,lineage_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,source_revision_id) REFERENCES project_revisions(tenant_id,id),
        FOREIGN KEY(tenant_id,seed_revision_id) REFERENCES project_revisions(tenant_id,id),
        FOREIGN KEY(tenant_id,principal_id) REFERENCES principals(tenant_id,id),
        FOREIGN KEY(tenant_id,workflow_run_id) REFERENCES workflow_runs(tenant_id,id)
    )""")
    op.execute("""CREATE TABLE document_merges (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL, document_id uuid NOT NULL,
        source_document_id uuid NOT NULL, source_revision_id uuid NOT NULL, source_state_version bigint NOT NULL,
        target_revision_id uuid NOT NULL, target_state_version bigint NOT NULL, common_revision_id uuid NOT NULL,
        mode text NOT NULL CHECK(mode IN ('parameters','source_geometry')),
        principal_id uuid NOT NULL, idempotency_key text NOT NULL, request_hash text NOT NULL,
        proposal jsonb NOT NULL, workflow_run_id uuid, created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tenant_id,principal_id,idempotency_key),
        FOREIGN KEY(tenant_id,document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,project_id,document_id,target_revision_id) REFERENCES project_revisions(tenant_id,project_id,branch_id,id),
        FOREIGN KEY(tenant_id,project_id,source_document_id,source_revision_id) REFERENCES project_revisions(tenant_id,project_id,branch_id,id),
        FOREIGN KEY(tenant_id,source_document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,source_revision_id) REFERENCES project_revisions(tenant_id,id),
        FOREIGN KEY(tenant_id,target_revision_id) REFERENCES project_revisions(tenant_id,id),
        FOREIGN KEY(tenant_id,common_revision_id) REFERENCES project_revisions(tenant_id,id),
        FOREIGN KEY(tenant_id,principal_id) REFERENCES principals(tenant_id,id),
        FOREIGN KEY(tenant_id,workflow_run_id) REFERENCES workflow_runs(tenant_id,id)
    )""")
    op.execute("""CREATE FUNCTION protect_document_branch_request() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF (to_jsonb(NEW)-'workflow_run_id') IS DISTINCT FROM (to_jsonb(OLD)-'workflow_run_id')
           OR (OLD.workflow_run_id IS NOT NULL AND NEW.workflow_run_id IS DISTINCT FROM OLD.workflow_run_id) THEN
            RAISE EXCEPTION 'document branch request is immutable';
        END IF; RETURN NEW; END $$""")
    for table in ('document_forks','document_merges'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f"""CREATE POLICY {table}_tenant ON {table}
            USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
            WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
        op.execute(f'GRANT SELECT,INSERT ON {table} TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator')
        op.execute(f'GRANT UPDATE(workflow_run_id) ON {table} TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator')
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION protect_document_branch_request()')
        op.execute(f'CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_document_evidence_mutation()')


def downgrade():
    op.execute('DROP TABLE document_merges')
    op.execute('DROP TABLE document_forks')
    op.execute('DROP FUNCTION protect_document_branch_request()')
