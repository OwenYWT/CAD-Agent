"""Immutable project revisions, branches, and candidate change sets.

Revision ID: 0003_revisions_and_changes
Revises: 0002_workflows_and_events
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_revisions_and_changes"
down_revision = "0002_workflows_and_events"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "project_branches",
    "project_revisions",
    "change_sets",
)


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True)


def _jsonb() -> sa.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # Allows source-workflow references to prove both tenant and project.
    op.create_unique_constraint(
        "uq_workflow_runs_tenant_project_id",
        "workflow_runs",
        ["tenant_id", "project_id", "id"],
    )
    op.create_table(
        "project_branches",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("head_revision_id", _uuid()),
        sa.Column(
            "next_revision_number",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("created_by_principal_id", _uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "id",
            name="uq_project_branches_tenant_project_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "name",
            name="uq_project_branches_project_name",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
            name="fk_project_branches_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_project_branches_creator",
        ),
        sa.CheckConstraint(
            "next_revision_number >= 1",
            name="ck_project_branches_next_revision",
        ),
    )
    op.create_table(
        "project_revisions",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("branch_id", _uuid(), nullable=False),
        sa.Column("parent_revision_id", _uuid()),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest", _jsonb(), nullable=False),
        sa.Column("source_workflow_run_id", _uuid()),
        sa.Column("created_by_principal_id", _uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "id",
            name="uq_project_revisions_tenant_project_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "branch_id",
            "id",
            name="uq_project_revisions_tenant_project_branch_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "branch_id",
            "revision_number",
            name="uq_project_revisions_branch_number",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "branch_id"],
            [
                "project_branches.tenant_id",
                "project_branches.project_id",
                "project_branches.id",
            ],
            ondelete="CASCADE",
            name="fk_project_revisions_branch",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "branch_id", "parent_revision_id"],
            [
                "project_revisions.tenant_id",
                "project_revisions.project_id",
                "project_revisions.branch_id",
                "project_revisions.id",
            ],
            name="fk_project_revisions_parent",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_workflow_run_id"],
            [
                "workflow_runs.tenant_id",
                "workflow_runs.project_id",
                "workflow_runs.id",
            ],
            name="fk_project_revisions_source_workflow",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_project_revisions_creator",
        ),
        sa.CheckConstraint(
            "revision_number >= 1",
            name="ck_project_revisions_number",
        ),
        sa.CheckConstraint(
            "kind IN ('initial', 'candidate', 'rollback')",
            name="ck_project_revisions_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'initial' AND parent_revision_id IS NULL) "
            "OR (kind <> 'initial' AND parent_revision_id IS NOT NULL)",
            name="ck_project_revisions_parent",
        ),
    )
    op.create_foreign_key(
        "fk_project_branches_head",
        "project_branches",
        "project_revisions",
        ["tenant_id", "project_id", "id", "head_revision_id"],
        ["tenant_id", "project_id", "branch_id", "id"],
    )
    op.create_table(
        "change_sets",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("branch_id", _uuid(), nullable=False),
        sa.Column("base_revision_id", _uuid(), nullable=False),
        sa.Column("candidate_revision_id", _uuid(), nullable=False),
        sa.Column("source_workflow_run_id", _uuid()),
        sa.Column("created_by_principal_id", _uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "idempotency_payload_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column(
            "change_summary",
            _jsonb(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "validation_summary",
            _jsonb(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "risk_summary",
            _jsonb(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending_review",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_change_sets_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_change_sets_idempotency",
        ),
        sa.UniqueConstraint(
            "candidate_revision_id",
            name="uq_change_sets_candidate_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "branch_id"],
            [
                "project_branches.tenant_id",
                "project_branches.project_id",
                "project_branches.id",
            ],
            ondelete="CASCADE",
            name="fk_change_sets_branch",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "branch_id", "base_revision_id"],
            [
                "project_revisions.tenant_id",
                "project_revisions.project_id",
                "project_revisions.branch_id",
                "project_revisions.id",
            ],
            name="fk_change_sets_base_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "branch_id", "candidate_revision_id"],
            [
                "project_revisions.tenant_id",
                "project_revisions.project_id",
                "project_revisions.branch_id",
                "project_revisions.id",
            ],
            name="fk_change_sets_candidate_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_workflow_run_id"],
            [
                "workflow_runs.tenant_id",
                "workflow_runs.project_id",
                "workflow_runs.id",
            ],
            name="fk_change_sets_source_workflow",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_change_sets_creator",
        ),
        sa.CheckConstraint(
            "base_revision_id <> candidate_revision_id",
            name="ck_change_sets_distinct_revisions",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'accepted', 'rejected', "
            "'changes_requested', 'superseded')",
            name="ck_change_sets_status",
        ),
    )
    op.create_index(
        "ix_project_branches_tenant_project",
        "project_branches",
        ["tenant_id", "project_id", "updated_at"],
    )
    op.create_index(
        "ix_change_sets_tenant_status",
        "change_sets",
        ["tenant_id", "status", "updated_at"],
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_project_revision_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'project_revisions are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_revisions_immutable
        BEFORE UPDATE OR DELETE ON project_revisions
        FOR EACH ROW EXECUTE FUNCTION reject_project_revision_mutation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_project_branch_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.id IS DISTINCT FROM OLD.id
               OR NEW.name IS DISTINCT FROM OLD.name
               OR NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'project branch identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_branch_identity_immutable
        BEFORE UPDATE ON project_branches
        FOR EACH ROW EXECUTE FUNCTION protect_project_branch_identity()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_change_set_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.branch_id IS DISTINCT FROM OLD.branch_id
               OR NEW.base_revision_id IS DISTINCT FROM OLD.base_revision_id
               OR NEW.candidate_revision_id IS DISTINCT FROM OLD.candidate_revision_id
               OR NEW.source_workflow_run_id IS DISTINCT FROM OLD.source_workflow_run_id
               OR NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.idempotency_payload_hash IS DISTINCT FROM OLD.idempotency_payload_hash
               OR NEW.objective IS DISTINCT FROM OLD.objective
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'change set identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER change_set_identity_immutable
        BEFORE UPDATE ON change_sets
        FOR EACH ROW EXECUTE FUNCTION protect_change_set_identity()
        """
    )

    for table in TENANT_TABLES:
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
        "GRANT SELECT, INSERT, UPDATE ON project_branches TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT ON project_revisions TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON change_sets TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT ON project_branches, project_revisions, change_sets "
        "TO cad_agent_worker"
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_project_branches_head",
        "project_branches",
        type_="foreignkey",
    )
    op.drop_table("change_sets")
    op.drop_table("project_revisions")
    op.drop_table("project_branches")
    op.execute("DROP FUNCTION IF EXISTS protect_project_branch_identity()")
    op.execute("DROP FUNCTION IF EXISTS protect_change_set_identity()")
    op.execute("DROP FUNCTION IF EXISTS reject_project_revision_mutation()")
    op.drop_constraint(
        "uq_workflow_runs_tenant_project_id",
        "workflow_runs",
        type_="unique",
    )
