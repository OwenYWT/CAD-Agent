"""Feature leases with server-clock expiration and tenant isolation."""
from alembic import op

revision = "0018_feature_leases"
down_revision = "0017_feature_annotations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE document_feature_leases (
        token uuid PRIMARY KEY, tenant_id uuid NOT NULL, document_id uuid NOT NULL,
        feature_id uuid NOT NULL, principal_id uuid NOT NULL, client_id uuid NOT NULL,
        revision_id uuid NOT NULL, expires_at timestamptz NOT NULL,
        UNIQUE(document_id,feature_id),
        FOREIGN KEY(tenant_id,document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,principal_id) REFERENCES principals(tenant_id,id),
        FOREIGN KEY(tenant_id,revision_id) REFERENCES project_revisions(tenant_id,id)
    )""")
    op.execute("ALTER TABLE document_feature_leases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_feature_leases FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY document_feature_leases_tenant ON document_feature_leases
        USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
        WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON document_feature_leases TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator")


def downgrade():
    op.execute("DROP TABLE document_feature_leases")
