"""Project-scoped paired bridges and fenced immutable-release delivery queue."""
from alembic import op

revision='0024_local_bridge'
down_revision='0023_document_releases'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE local_bridges(
        id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,document_id uuid NOT NULL,
        created_by uuid NOT NULL,label text NOT NULL CHECK(length(label) BETWEEN 1 AND 120),
        pair_hash text,token_hash text,pair_expires_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '10 minutes',
        paired_at timestamptz,last_seen_at timestamptz,revoked_at timestamptz,
        client_info jsonb NOT NULL DEFAULT '{}'::jsonb,created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tenant_id,id),UNIQUE(tenant_id,project_id,id),
        FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id),
        FOREIGN KEY(tenant_id,document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,created_by) REFERENCES principals(tenant_id,id)
    )''')
    op.execute('''CREATE TABLE bridge_deliveries(
        id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,document_id uuid NOT NULL,bridge_id uuid NOT NULL,
        release_id uuid NOT NULL,requested_by uuid NOT NULL,archive_artifact_id uuid NOT NULL,
        archive_sha256 text NOT NULL,archive_size_bytes bigint NOT NULL CHECK(archive_size_bytes>0),
        manifest_sha256 text NOT NULL,manifest jsonb NOT NULL,
        status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','leased','delivered','failed','cancelled')),
        lease_token uuid,lease_expires_at timestamptz,attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
        receipt jsonb,error_message text,created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,finished_at timestamptz,
        FOREIGN KEY(tenant_id,project_id,bridge_id) REFERENCES local_bridges(tenant_id,project_id,id),
        FOREIGN KEY(tenant_id,document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,document_id,release_id) REFERENCES document_engineering_tasks(tenant_id,document_id,workflow_run_id),
        FOREIGN KEY(release_id) REFERENCES document_releases(workflow_run_id),
        FOREIGN KEY(tenant_id,archive_artifact_id) REFERENCES artifacts(tenant_id,id),
        FOREIGN KEY(tenant_id,requested_by) REFERENCES principals(tenant_id,id)
    )''')
    op.execute('''CREATE FUNCTION guard_bridge_delivery_source() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF (to_jsonb(NEW)-ARRAY['status','lease_token','lease_expires_at','attempts','receipt','error_message','finished_at'])
            IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','lease_token','lease_expires_at','attempts','receipt','error_message','finished_at'])
            OR OLD.status IN ('delivered','failed','cancelled') THEN
            RAISE EXCEPTION 'bridge delivery source and terminal evidence are immutable';
        END IF; RETURN NEW; END $$''')
    op.execute('CREATE TRIGGER bridge_delivery_source BEFORE UPDATE ON bridge_deliveries FOR EACH ROW EXECUTE FUNCTION guard_bridge_delivery_source()')
    for table in ('local_bridges','bridge_deliveries'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY {table}_tenant ON {table}
            USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
            WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)''')
        op.execute(f'GRANT SELECT,INSERT,UPDATE ON {table} TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator')
    op.execute('CREATE INDEX bridge_delivery_poll ON bridge_deliveries(bridge_id,status,created_at)')
    op.execute("CREATE UNIQUE INDEX bridge_delivery_idempotency ON bridge_deliveries(tenant_id,bridge_id,release_id) WHERE status NOT IN ('failed','cancelled')")


def downgrade():
    op.execute('DROP TABLE bridge_deliveries')
    op.execute('DROP FUNCTION guard_bridge_delivery_source()')
    op.execute('DROP TABLE local_bridges')
