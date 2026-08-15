"""Immutable generated source records for durable modeling steps.

Revision ID: 0008_agent_generated_sources
Revises: 0007_agent_candidate_builds
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_agent_generated_sources"
down_revision = "0007_agent_candidate_builds"
branch_labels = None
depends_on = None


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "agent_generated_sources",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("candidate_build_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("step_run_id", _uuid(), nullable=False),
        sa.Column("predecessor_source_id", _uuid()),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("generator_kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("provider_response_id", sa.Text()),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_hash", sa.String(length=64), nullable=False),
        sa.Column("finish_reason", sa.Text()),
        sa.Column(
            "usage",
            postgresql.JSONB(),
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
            "tenant_id", "id", name="uq_agent_generated_sources_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "step_run_id", name="uq_agent_generated_sources_step"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_build_id"],
            ["agent_candidate_builds.tenant_id", "agent_candidate_builds.id"],
            ondelete="CASCADE",
            name="fk_agent_generated_sources_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_run_id", "step_run_id"],
            ["step_runs.tenant_id", "step_runs.workflow_run_id", "step_runs.id"],
            name="fk_agent_generated_sources_step",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "predecessor_source_id"],
            ["agent_generated_sources.tenant_id", "agent_generated_sources.id"],
            name="fk_agent_generated_sources_predecessor",
        ),
        sa.CheckConstraint(
            "length(source_code) > 0", name="ck_agent_generated_sources_code"
        ),
    )
    op.execute(
        'ALTER TABLE "agent_generated_sources" ENABLE ROW LEVEL SECURITY'
    )
    op.execute(
        'ALTER TABLE "agent_generated_sources" FORCE ROW LEVEL SECURITY'
    )
    op.execute(
        'CREATE POLICY "agent_generated_sources_tenant_isolation" '
        'ON "agent_generated_sources" '
        "TO cad_agent_runtime, cad_agent_worker, cad_agent_migrator "
        "USING (tenant_id = "
        "NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = "
        "NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE FUNCTION reject_agent_generated_sources_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'agent_generated_sources rows are immutable'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER agent_generated_sources_immutable "
        "BEFORE UPDATE OR DELETE ON agent_generated_sources FOR EACH ROW "
        "EXECUTE FUNCTION reject_agent_generated_sources_mutation()"
    )
    op.execute(
        "GRANT SELECT, INSERT ON agent_generated_sources "
        "TO cad_agent_runtime, cad_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON agent_generated_sources "
        "TO cad_agent_migrator"
    )


def downgrade() -> None:
    op.drop_table("agent_generated_sources")
    op.execute("DROP FUNCTION IF EXISTS reject_agent_generated_sources_mutation()")
