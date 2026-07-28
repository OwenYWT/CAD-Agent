"""Two-phase immutable artifact metadata.

Revision ID: 0004_artifacts
Revises: 0003_revisions_and_changes
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0004_artifacts"
down_revision = "0003_revisions_and_changes"
branch_labels = None
depends_on = None


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True)


def _jsonb() -> sa.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_execution_attempts_tenant_workflow_id",
        "execution_attempts",
        ["tenant_id", "workflow_run_id", "id"],
    )
    op.create_table(
        "artifact_uploads",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("revision_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("attempt_id", _uuid(), nullable=False),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("declared_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("declared_sha256", sa.String(length=64), nullable=False),
        sa.Column("staging_object_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="authorized"),
        sa.Column("rejection_code", sa.Text()),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_artifact_uploads_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "staging_object_key",
            name="uq_artifact_uploads_staging_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "revision_id"],
            [
                "project_revisions.tenant_id",
                "project_revisions.project_id",
                "project_revisions.id",
            ],
            name="fk_artifact_uploads_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_run_id", "attempt_id"],
            [
                "execution_attempts.tenant_id",
                "execution_attempts.workflow_run_id",
                "execution_attempts.id",
            ],
            name="fk_artifact_uploads_attempt",
        ),
        sa.CheckConstraint(
            "declared_size_bytes >= 0",
            name="ck_artifact_uploads_size",
        ),
        sa.CheckConstraint(
            "status IN ('authorized', 'committed', 'rejected', 'expired')",
            name="ck_artifact_uploads_status",
        ),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("revision_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("attempt_id", _uuid(), nullable=False),
        sa.Column("upload_id", _uuid(), nullable=False),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column(
            "runtime_metadata",
            _jsonb(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_artifacts_tenant_id_id",
        ),
        sa.UniqueConstraint("upload_id", name="uq_artifacts_upload"),
        sa.UniqueConstraint("object_key", name="uq_artifacts_object_key"),
        sa.UniqueConstraint(
            "tenant_id",
            "revision_id",
            "filename",
            name="uq_artifacts_revision_filename",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "revision_id"],
            [
                "project_revisions.tenant_id",
                "project_revisions.project_id",
                "project_revisions.id",
            ],
            name="fk_artifacts_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_run_id", "attempt_id"],
            [
                "execution_attempts.tenant_id",
                "execution_attempts.workflow_run_id",
                "execution_attempts.id",
            ],
            name="fk_artifacts_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "upload_id"],
            ["artifact_uploads.tenant_id", "artifact_uploads.id"],
            name="fk_artifacts_upload",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifacts_size"),
    )
    op.create_index(
        "ix_artifact_uploads_expiry",
        "artifact_uploads",
        ["tenant_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_artifacts_revision",
        "artifacts",
        ["tenant_id", "project_id", "revision_id", "created_at"],
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_artifact_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'artifacts are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER artifacts_immutable
        BEFORE UPDATE OR DELETE ON artifacts
        FOR EACH ROW EXECUTE FUNCTION reject_artifact_mutation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_artifact_upload_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.revision_id IS DISTINCT FROM OLD.revision_id
               OR NEW.workflow_run_id IS DISTINCT FROM OLD.workflow_run_id
               OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
               OR NEW.artifact_kind IS DISTINCT FROM OLD.artifact_kind
               OR NEW.filename IS DISTINCT FROM OLD.filename
               OR NEW.content_type IS DISTINCT FROM OLD.content_type
               OR NEW.declared_size_bytes IS DISTINCT FROM OLD.declared_size_bytes
               OR NEW.declared_sha256 IS DISTINCT FROM OLD.declared_sha256
               OR NEW.staging_object_key IS DISTINCT FROM OLD.staging_object_key
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'artifact upload identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER artifact_upload_identity_immutable
        BEFORE UPDATE ON artifact_uploads
        FOR EACH ROW EXECUTE FUNCTION protect_artifact_upload_identity()
        """
    )

    for table in ("artifact_uploads", "artifacts"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            "USING (tenant_id = "
            "NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            "WITH CHECK (tenant_id = "
            "NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON artifact_uploads TO cad_agent_runtime"
    )
    op.execute("GRANT SELECT, INSERT ON artifacts TO cad_agent_runtime")
    op.execute(
        "GRANT SELECT ON artifact_uploads, artifacts TO cad_agent_worker"
    )


def downgrade() -> None:
    op.drop_table("artifacts")
    op.drop_table("artifact_uploads")
    op.execute("DROP FUNCTION IF EXISTS protect_artifact_upload_identity()")
    op.execute("DROP FUNCTION IF EXISTS reject_artifact_mutation()")
    op.drop_constraint(
        "uq_execution_attempts_tenant_workflow_id",
        "execution_attempts",
        type_="unique",
    )
