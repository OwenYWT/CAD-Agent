"""Version derived checkpoints without rewriting immutable historical evidence."""
from alembic import op

revision = "0025_semantic_checkpoints"
down_revision = "0024_local_bridge"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE document_checkpoints ADD COLUMN projector_version integer NOT NULL DEFAULT 1 CHECK(projector_version > 0)")
    op.execute("ALTER TABLE document_checkpoints DROP CONSTRAINT document_checkpoints_pkey")
    op.execute("ALTER TABLE document_checkpoints ADD PRIMARY KEY(tenant_id, document_id, revision_id, projector_version)")


def downgrade():
    # Refuse to discard a historical projection to make a downgrade fit the old
    # key. The caller must explicitly archive/select versions before downgrading.
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM document_checkpoints
        GROUP BY tenant_id,document_id,revision_id HAVING COUNT(*)>1) THEN
        RAISE EXCEPTION 'Multiple immutable projection versions exist; archive them before downgrade';
        END IF; END $$""")
    op.execute("ALTER TABLE document_checkpoints DROP CONSTRAINT document_checkpoints_pkey")
    op.execute("ALTER TABLE document_checkpoints DROP COLUMN projector_version")
    op.execute("ALTER TABLE document_checkpoints ADD PRIMARY KEY(tenant_id, document_id, revision_id)")
