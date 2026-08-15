"""Durable Agent candidate builds, staging manifests, evidence, and seals.

Revision ID: 0007_agent_candidate_builds
Revises: 0006_legacy_product_stores
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_agent_candidate_builds"
down_revision = "0006_legacy_product_stores"
branch_labels = None
depends_on = None


TABLES = (
    "agent_candidate_builds",
    "agent_staging_manifests",
    "agent_validation_evidence",
    "agent_candidate_seals",
)


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True)


def _jsonb() -> sa.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text())


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_execution_attempts_tenant_workflow_step_id",
        "execution_attempts",
        ["tenant_id", "workflow_run_id", "step_run_id", "id"],
    )
    op.create_table(
        "agent_candidate_builds",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("branch_id", _uuid(), nullable=False),
        sa.Column("base_revision_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("created_by_principal_id", _uuid(), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="building"),
        sa.Column("failure_code", sa.Text()),
        sa.Column("failure_message", sa.Text()),
        sa.Column("candidate_revision_id", _uuid()),
        sa.Column("change_set_id", _uuid()),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_agent_candidate_builds_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_run_id",
            name="uq_agent_candidate_builds_workflow",
        ),
        sa.UniqueConstraint("candidate_revision_id"),
        sa.UniqueConstraint("change_set_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
            name="fk_agent_candidate_builds_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "branch_id", "base_revision_id"],
            [
                "project_revisions.tenant_id",
                "project_revisions.project_id",
                "project_revisions.branch_id",
                "project_revisions.id",
            ],
            name="fk_agent_candidate_builds_base_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "workflow_run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.project_id", "workflow_runs.id"],
            ondelete="CASCADE",
            name="fk_agent_candidate_builds_workflow",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_agent_candidate_builds_creator",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "candidate_revision_id"],
            ["project_revisions.tenant_id", "project_revisions.project_id", "project_revisions.id"],
            name="fk_agent_candidate_builds_candidate_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "change_set_id"],
            ["change_sets.tenant_id", "change_sets.id"],
            name="fk_agent_candidate_builds_change_set",
        ),
        sa.CheckConstraint(
            "status IN ('building', 'reviewable', 'failed', 'cancelled', 'abandoned')",
            name="ck_agent_candidate_builds_status",
        ),
        sa.CheckConstraint(
            "(candidate_revision_id IS NULL) = (change_set_id IS NULL)",
            name="ck_agent_candidate_builds_product_links",
        ),
        sa.CheckConstraint(
            "status <> 'reviewable' OR candidate_revision_id IS NOT NULL",
            name="ck_agent_candidate_builds_reviewable_links",
        ),
    )
    op.create_table(
        "agent_staging_manifests",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("candidate_build_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("step_run_id", _uuid(), nullable=False),
        sa.Column("execution_attempt_id", _uuid(), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest", _jsonb(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="accepted"),
        sa.Column("supersedes_id", _uuid()),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_agent_staging_manifests_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "execution_attempt_id",
            name="uq_agent_staging_manifest_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_build_id"],
            ["agent_candidate_builds.tenant_id", "agent_candidate_builds.id"],
            ondelete="CASCADE",
            name="fk_agent_staging_manifests_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_run_id", "step_run_id", "execution_attempt_id"],
            [
                "execution_attempts.tenant_id",
                "execution_attempts.workflow_run_id",
                "execution_attempts.step_run_id",
                "execution_attempts.id",
            ],
            name="fk_agent_staging_manifests_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supersedes_id"],
            ["agent_staging_manifests.tenant_id", "agent_staging_manifests.id"],
            name="fk_agent_staging_manifests_supersedes",
        ),
        sa.CheckConstraint(
            "lease_generation >= 1",
            name="ck_agent_staging_manifests_generation",
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'rejected')",
            name="ck_agent_staging_manifests_status",
        ),
    )
    op.create_table(
        "agent_validation_evidence",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("candidate_build_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("step_run_id", _uuid(), nullable=False),
        sa.Column("execution_attempt_id", _uuid()),
        sa.Column("staging_manifest_id", _uuid(), nullable=False),
        sa.Column("gate", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence", _jsonb(), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_agent_validation_evidence_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_build_id",
            "staging_manifest_id",
            "gate",
            "evidence_hash",
            name="uq_agent_validation_evidence_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_build_id"],
            ["agent_candidate_builds.tenant_id", "agent_candidate_builds.id"],
            ondelete="CASCADE",
            name="fk_agent_validation_evidence_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_run_id", "step_run_id"],
            ["step_runs.tenant_id", "step_runs.workflow_run_id", "step_runs.id"],
            name="fk_agent_validation_evidence_step",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "workflow_run_id",
                "step_run_id",
                "execution_attempt_id",
            ],
            [
                "execution_attempts.tenant_id",
                "execution_attempts.workflow_run_id",
                "execution_attempts.step_run_id",
                "execution_attempts.id",
            ],
            name="fk_agent_validation_evidence_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "staging_manifest_id"],
            ["agent_staging_manifests.tenant_id", "agent_staging_manifests.id"],
            name="fk_agent_validation_evidence_manifest",
        ),
        sa.CheckConstraint(
            "gate IN ('artifact_integrity', 'geometry', 'visual', 'dfm')",
            name="ck_agent_validation_evidence_gate",
        ),
        sa.CheckConstraint(
            "mode IN ('required', 'advisory')",
            name="ck_agent_validation_evidence_mode",
        ),
        sa.CheckConstraint(
            "outcome IN ('passed', 'failed', 'indeterminate')",
            name="ck_agent_validation_evidence_outcome",
        ),
    )
    op.create_table(
        "agent_candidate_seals",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("candidate_build_id", _uuid(), nullable=False),
        sa.Column("seal_key", sa.Text(), nullable=False),
        sa.Column("selection_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("result", _jsonb()),
        sa.Column("error_code", sa.Text()),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_agent_candidate_seals_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "candidate_build_id", name="uq_agent_candidate_seal_build"
        ),
        sa.UniqueConstraint("tenant_id", "seal_key", name="uq_agent_candidate_seal_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_build_id"],
            ["agent_candidate_builds.tenant_id", "agent_candidate_builds.id"],
            ondelete="CASCADE",
            name="fk_agent_candidate_seals_candidate",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'copying', 'committed', 'failed')",
            name="ck_agent_candidate_seals_status",
        ),
    )

    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            "TO cad_agent_runtime, cad_agent_worker, cad_agent_migrator "
            "USING (tenant_id = "
            "NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            "WITH CHECK (tenant_id = "
            "NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        )

    for table in ("agent_staging_manifests", "agent_validation_evidence"):
        op.execute(
            f"CREATE FUNCTION reject_{table}_mutation() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            f"RAISE EXCEPTION '{table} rows are immutable'; END; $$"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_{table}_mutation()"
        )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON agent_candidate_builds, "
        "agent_candidate_seals TO cad_agent_runtime, cad_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT ON agent_staging_manifests, "
        "agent_validation_evidence TO cad_agent_runtime, cad_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        + ", ".join(TABLES)
        + " TO cad_agent_migrator"
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    for table in ("agent_staging_manifests", "agent_validation_evidence"):
        op.execute(f"DROP FUNCTION IF EXISTS reject_{table}_mutation()")
    op.drop_constraint(
        "uq_execution_attempts_tenant_workflow_step_id",
        "execution_attempts",
        type_="unique",
    )
