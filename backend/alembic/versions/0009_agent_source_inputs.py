"""Immutable many-to-one generated-source provenance edges.

Revision ID: 0009_agent_source_inputs
Revises: 0008_agent_generated_sources
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_agent_source_inputs"
down_revision = "0008_agent_generated_sources"
branch_labels = None
depends_on = None


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "agent_generated_source_inputs",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("source_id", _uuid(), nullable=False),
        sa.Column("input_source_id", _uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "source_id",
            "input_source_id",
            name="pk_agent_generated_source_inputs",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source_id",
            "ordinal",
            name="uq_agent_generated_source_input_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["agent_generated_sources.tenant_id", "agent_generated_sources.id"],
            ondelete="CASCADE",
            name="fk_agent_generated_source_inputs_source",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "input_source_id"],
            ["agent_generated_sources.tenant_id", "agent_generated_sources.id"],
            name="fk_agent_generated_source_inputs_input",
        ),
        sa.CheckConstraint(
            "source_id <> input_source_id",
            name="ck_agent_generated_source_inputs_not_self",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="ck_agent_generated_source_inputs_ordinal"
        ),
    )
    op.execute(
        'ALTER TABLE "agent_generated_source_inputs" ENABLE ROW LEVEL SECURITY'
    )
    op.execute(
        'ALTER TABLE "agent_generated_source_inputs" FORCE ROW LEVEL SECURITY'
    )
    op.execute(
        'CREATE POLICY "agent_generated_source_inputs_tenant_isolation" '
        'ON "agent_generated_source_inputs" '
        "TO cad_agent_runtime, cad_agent_worker, cad_agent_migrator "
        "USING (tenant_id = "
        "NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = "
        "NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE FUNCTION reject_agent_generated_source_inputs_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'agent_generated_source_inputs rows are immutable'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER agent_generated_source_inputs_immutable "
        "BEFORE UPDATE OR DELETE ON agent_generated_source_inputs FOR EACH ROW "
        "EXECUTE FUNCTION reject_agent_generated_source_inputs_mutation()"
    )
    op.execute(
        "GRANT SELECT, INSERT ON agent_generated_source_inputs "
        "TO cad_agent_runtime, cad_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON agent_generated_source_inputs "
        "TO cad_agent_migrator"
    )


def downgrade() -> None:
    op.drop_table("agent_generated_source_inputs")
    op.execute(
        "DROP FUNCTION IF EXISTS reject_agent_generated_source_inputs_mutation()"
    )
