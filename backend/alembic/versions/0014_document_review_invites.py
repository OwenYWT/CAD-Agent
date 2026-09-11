"""Single-use, expiring project viewer invitations for cloud review.

Revision ID: 0014_document_review_invites
Revises: 0013_cloud_documents
"""
from alembic import op

revision = "0014_document_review_invites"
down_revision = "0013_cloud_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE document_review_invites (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, document_id uuid NOT NULL,
        token_hash text NOT NULL UNIQUE, created_by uuid NOT NULL,
        expires_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '1 day',
        used_by uuid, used_at timestamptz, created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id, document_id) REFERENCES cloud_documents(tenant_id, id),
        FOREIGN KEY (tenant_id, created_by) REFERENCES principals(tenant_id, id),
        FOREIGN KEY (tenant_id, used_by) REFERENCES principals(tenant_id, id)
    )""")
    op.execute("ALTER TABLE document_review_invites ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_review_invites FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY document_review_invites_tenant ON document_review_invites
        TO cad_agent_runtime, cad_agent_migrator
        USING (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
        WITH CHECK (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON document_review_invites TO cad_agent_runtime, cad_agent_migrator")


def downgrade():
    op.execute("DROP TABLE document_review_invites")
