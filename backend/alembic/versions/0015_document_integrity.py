"""Tenant-safe revision links and transactional operation review status.

Revision ID: 0015_document_integrity
Revises: 0014_document_review_invites
"""
from alembic import op

revision = "0015_document_integrity"
down_revision = "0014_document_review_invites"
branch_labels = None
depends_on = None

LINKS = (
    ("document_checkpoints", "revision_id", "project_revisions"),
    ("document_comments", "revision_id", "project_revisions"),
    ("cad_operations", "base_revision_id", "project_revisions"),
    ("cad_operations", "result_revision_id", "project_revisions"),
    ("cad_operations", "id", "workflow_runs"),
)


def upgrade():
    op.execute("ALTER TABLE project_revisions ADD CONSTRAINT uq_revision_tenant_id UNIQUE(tenant_id, id)")
    for table, column, target in LINKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_{column}_tenant FOREIGN KEY (tenant_id,{column}) REFERENCES {target}(tenant_id,id)")
    op.execute("""CREATE FUNCTION sync_document_review_status() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status IN ('committed','rejected','changes_requested','rolled_back') THEN
                UPDATE cad_operations SET status=CASE WHEN NEW.status='changes_requested' THEN 'rejected' ELSE NEW.status END,
                    result_revision_id=NEW.candidate_revision_id,
                    finished_at=COALESCE(finished_at,CURRENT_TIMESTAMP)
                WHERE tenant_id=NEW.tenant_id AND id=NEW.source_workflow_run_id;
            END IF;
            RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER document_review_status AFTER UPDATE OF status ON change_sets
        FOR EACH ROW EXECUTE FUNCTION sync_document_review_status()""")
    op.execute("""CREATE FUNCTION reject_document_evidence_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'document evidence is immutable'; END; $$""")
    for table in ("document_checkpoints", "document_events", "document_comments"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_document_evidence_mutation()")


def downgrade():
    for table in ("document_checkpoints", "document_events", "document_comments"):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
    op.execute("DROP FUNCTION reject_document_evidence_mutation()")
    op.execute("DROP TRIGGER document_review_status ON change_sets")
    op.execute("DROP FUNCTION sync_document_review_status()")
    for table, column, _ in LINKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_{column}_tenant")
    op.execute("ALTER TABLE project_revisions DROP CONSTRAINT uq_revision_tenant_id")
