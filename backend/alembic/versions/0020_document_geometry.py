"""Immutable component geometry cache and revision-linked scene projections."""
from alembic import op

revision = '0020_document_geometry'
down_revision = '0019_editor_invitations'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE document_geometry_blobs (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL,
        geometry_sha256 text NOT NULL CHECK(geometry_sha256 ~ '^[a-f0-9]{64}$'),
        runtime_digest text NOT NULL, lod text NOT NULL CHECK(lod IN ('coarse','medium','fine')),
        sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'), size_bytes bigint NOT NULL CHECK(size_bytes>0),
        object_key text NOT NULL, metadata jsonb NOT NULL,
        UNIQUE(tenant_id,project_id,geometry_sha256,runtime_digest,lod),
        FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id)
    )""")
    op.execute("""CREATE TABLE document_scenes (
        tenant_id uuid NOT NULL, document_id uuid NOT NULL, revision_id uuid NOT NULL,
        runtime_digest text NOT NULL, projection jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(document_id,revision_id,runtime_digest),
        FOREIGN KEY(tenant_id,document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,revision_id) REFERENCES project_revisions(tenant_id,id)
    )""")
    for table in ('document_geometry_blobs', 'document_scenes'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f"""CREATE POLICY {table}_tenant ON {table}
            USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
            WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
        op.execute(f'GRANT SELECT,INSERT ON {table} TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator')


def downgrade():
    op.execute('DROP TABLE document_scenes')
    op.execute('DROP TABLE document_geometry_blobs')
