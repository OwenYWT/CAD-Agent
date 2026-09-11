"""Branch-backed cloud documents, serialized operations and collaboration.

Revision ID: 0013_cloud_documents
Revises: 0012_native_bom_seal_gate
"""
from alembic import op

revision = "0013_cloud_documents"
down_revision = "0012_native_bom_seal_gate"
branch_labels = None
depends_on = None


def upgrade():
    # A document ID is its branch ID. The branch is the single head authority.
    op.execute("""
        CREATE TABLE cloud_documents (
            id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL,
            head_revision_id uuid NOT NULL, state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
            event_sequence bigint NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, project_id, id) REFERENCES project_branches(tenant_id, project_id, id),
            FOREIGN KEY (tenant_id, project_id, id, head_revision_id)
                REFERENCES project_revisions(tenant_id, project_id, branch_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE document_checkpoints (
            tenant_id uuid NOT NULL, document_id uuid NOT NULL, revision_id uuid NOT NULL,
            projection jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, document_id, revision_id),
            FOREIGN KEY (tenant_id, document_id) REFERENCES cloud_documents(tenant_id, id),
            FOREIGN KEY (revision_id) REFERENCES project_revisions(id)
        )
    """)
    op.execute("""
        CREATE TABLE cad_operations (
            id uuid PRIMARY KEY REFERENCES workflow_runs(id), tenant_id uuid NOT NULL,
            document_id uuid NOT NULL, actor_principal_id uuid NOT NULL,
            actor text NOT NULL CHECK (actor IN ('user', 'agent')),
            base_revision_id uuid NOT NULL REFERENCES project_revisions(id),
            base_state_version bigint NOT NULL CHECK (base_state_version >= 0),
            action text NOT NULL, arguments jsonb NOT NULL,
            idempotency_key text NOT NULL, request_hash text NOT NULL,
            status text NOT NULL DEFAULT 'queued' CHECK
                (status IN ('queued', 'running', 'reviewable', 'committed', 'rejected', 'failed', 'cancelled', 'rolled_back')),
            result_revision_id uuid REFERENCES project_revisions(id), error_code text,
            queue_number bigint GENERATED ALWAYS AS IDENTITY,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at timestamptz, finished_at timestamptz,
            UNIQUE (tenant_id, idempotency_key),
            FOREIGN KEY (tenant_id, document_id) REFERENCES cloud_documents(tenant_id, id),
            FOREIGN KEY (tenant_id, actor_principal_id) REFERENCES principals(tenant_id, id)
        )
    """)
    op.execute("CREATE UNIQUE INDEX cad_operations_one_running ON cad_operations(document_id) WHERE status='running'")
    op.execute("CREATE INDEX cad_operations_queue ON cad_operations(document_id, queue_number) WHERE status IN ('queued', 'running')")
    op.execute("""
        CREATE TABLE document_events (
            tenant_id uuid NOT NULL, document_id uuid NOT NULL, sequence bigint NOT NULL,
            event_type text NOT NULL, payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, document_id, sequence),
            FOREIGN KEY (tenant_id, document_id) REFERENCES cloud_documents(tenant_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE document_comments (
            id uuid PRIMARY KEY, tenant_id uuid NOT NULL, document_id uuid NOT NULL,
            principal_id uuid NOT NULL, revision_id uuid NOT NULL REFERENCES project_revisions(id),
            feature_id uuid, body text NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (tenant_id, document_id) REFERENCES cloud_documents(tenant_id, id),
            FOREIGN KEY (tenant_id, principal_id) REFERENCES principals(tenant_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE document_presence (
            tenant_id uuid NOT NULL, document_id uuid NOT NULL, client_id uuid NOT NULL,
            principal_id uuid NOT NULL, selected_feature_id uuid,
            last_seen_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, document_id, client_id),
            FOREIGN KEY (tenant_id, document_id) REFERENCES cloud_documents(tenant_id, id),
            FOREIGN KEY (tenant_id, principal_id) REFERENCES principals(tenant_id, id)
        )
    """)
    op.execute("""
        INSERT INTO cloud_documents(id, tenant_id, project_id, head_revision_id)
        SELECT id, tenant_id, project_id, head_revision_id FROM project_branches
        WHERE head_revision_id IS NOT NULL
    """)
    # Covers commit, rollback and the existing initial-branch API atomically.
    op.execute("""
        CREATE FUNCTION sync_cloud_document_head() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE seq bigint; ver bigint;
        BEGIN
            INSERT INTO cloud_documents(id, tenant_id, project_id, head_revision_id)
            VALUES (NEW.id, NEW.tenant_id, NEW.project_id, NEW.head_revision_id)
            ON CONFLICT(id) DO UPDATE SET head_revision_id=EXCLUDED.head_revision_id,
                state_version=cloud_documents.state_version+1,
                event_sequence=cloud_documents.event_sequence+1, updated_at=CURRENT_TIMESTAMP
            RETURNING event_sequence, state_version INTO seq, ver;
            IF seq > 0 THEN
                INSERT INTO document_events(tenant_id, document_id, sequence, event_type, payload)
                VALUES(NEW.tenant_id, NEW.id, seq, 'state_delta', jsonb_build_object(
                    'base_revision_id', OLD.head_revision_id, 'revision_id', NEW.head_revision_id,
                    'base_state_version', ver-1, 'state_version', ver));
            END IF;
            RETURN NEW;
        END; $$
    """)
    op.execute("""
        CREATE TRIGGER cloud_document_head AFTER UPDATE OF head_revision_id ON project_branches
        FOR EACH ROW WHEN (NEW.head_revision_id IS NOT NULL AND OLD.head_revision_id IS DISTINCT FROM NEW.head_revision_id)
        EXECUTE FUNCTION sync_cloud_document_head()
    """)
    for table in ("cloud_documents", "document_checkpoints", "cad_operations", "document_events", "document_comments", "document_presence"):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f"""CREATE POLICY {table}_tenant ON {table}
            TO cad_agent_runtime, cad_agent_worker, cad_agent_migrator
            USING (tenant_id=NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id=NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO cad_agent_runtime, cad_agent_worker, cad_agent_migrator")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE cad_operations_queue_number_seq TO cad_agent_runtime, cad_agent_worker, cad_agent_migrator")


def downgrade():
    op.execute("DROP TRIGGER cloud_document_head ON project_branches")
    op.execute("DROP FUNCTION sync_cloud_document_head()")
    for table in ("document_presence", "document_comments", "document_events", "cad_operations", "document_checkpoints", "cloud_documents"):
        op.execute(f"DROP TABLE {table}")
