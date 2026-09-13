"""Durable, deduplicated read-only scene derivations with immutable provenance."""
from alembic import op

revision = '0027_document_scene_tasks'
down_revision = '0026_change_set_base_generation'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE document_scenes ADD COLUMN scene_schema text NOT NULL DEFAULT 'cad-scene.v1'")
    op.execute('ALTER TABLE document_scenes DROP CONSTRAINT document_scenes_pkey')
    op.execute('ALTER TABLE document_scenes ADD PRIMARY KEY(document_id,revision_id,runtime_digest,scene_schema)')
    op.execute('''CREATE TABLE document_scene_tasks (
        workflow_run_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL,
        document_id uuid NOT NULL, revision_id uuid NOT NULL, source_artifact_id uuid NOT NULL,
        source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[a-f0-9]{64}$'),
        principal_id uuid NOT NULL, runtime_digest text NOT NULL, scene_schema text NOT NULL,
        request_number bigint NOT NULL CHECK(request_number >= 0), created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tenant_id,document_id,revision_id,runtime_digest,scene_schema,request_number),
        FOREIGN KEY(tenant_id,workflow_run_id) REFERENCES workflow_runs(tenant_id,id),
        FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id),
        FOREIGN KEY(tenant_id,document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,revision_id) REFERENCES project_revisions(tenant_id,id),
        FOREIGN KEY(tenant_id,source_artifact_id) REFERENCES artifacts(tenant_id,id),
        FOREIGN KEY(tenant_id,principal_id) REFERENCES principals(tenant_id,id)
    )''')
    op.execute('ALTER TABLE document_scene_tasks ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE document_scene_tasks FORCE ROW LEVEL SECURITY')
    op.execute("""CREATE POLICY document_scene_tasks_tenant ON document_scene_tasks
        USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
        WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
    op.execute('GRANT SELECT,INSERT ON document_scene_tasks TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator')
    op.execute('CREATE TRIGGER document_scene_tasks_immutable BEFORE UPDATE OR DELETE ON document_scene_tasks FOR EACH ROW EXECUTE FUNCTION reject_document_evidence_mutation()')


def downgrade():
    # Do not collapse distinct schema evidence into one historical cache row.
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM document_scenes WHERE scene_schema<>'cad-scene.v1') THEN
        RAISE EXCEPTION 'Archive newer scene schema evidence before downgrade'; END IF; END $$""")
    op.execute('DROP TABLE document_scene_tasks')
    op.execute('ALTER TABLE document_scenes DROP CONSTRAINT document_scenes_pkey')
    op.execute('ALTER TABLE document_scenes ADD PRIMARY KEY(document_id,revision_id,runtime_digest)')
    op.execute('ALTER TABLE document_scenes DROP COLUMN scene_schema')
