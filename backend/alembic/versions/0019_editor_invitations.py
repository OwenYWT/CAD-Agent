"""Explicit editor invitations without granting tenant or project ownership."""
from alembic import op

revision = "0019_editor_invitations"
down_revision = "0018_feature_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE document_review_invites ADD COLUMN role text NOT NULL DEFAULT 'viewer' CHECK(role IN ('viewer','editor'))")


def downgrade():
    op.execute("ALTER TABLE document_review_invites DROP COLUMN role")
