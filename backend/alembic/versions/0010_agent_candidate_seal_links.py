"""Immutable selected-manifest and evidence links for candidate seals.

Revision ID: 0010_agent_candidate_seal_links
Revises: 0009_agent_source_inputs
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_agent_candidate_seal_links"
down_revision = "0009_agent_source_inputs"
branch_labels = None
depends_on = None


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True)


TABLES = ("agent_seal_manifests", "agent_seal_evidence")


def upgrade() -> None:
    op.add_column(
        "agent_candidate_seals",
        sa.Column("selection", postgresql.JSONB(), nullable=True),
    )
    op.create_table(
        "agent_seal_manifests",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("seal_id", _uuid(), nullable=False),
        sa.Column("staging_manifest_id", _uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("plan_step_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "seal_id",
            "staging_manifest_id",
            name="pk_agent_seal_manifests",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "staging_manifest_id",
            name="uq_agent_seal_manifest_consumed",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "seal_id",
            "ordinal",
            name="uq_agent_seal_manifest_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "seal_id"],
            ["agent_candidate_seals.tenant_id", "agent_candidate_seals.id"],
            ondelete="CASCADE",
            name="fk_agent_seal_manifests_seal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "staging_manifest_id"],
            ["agent_staging_manifests.tenant_id", "agent_staging_manifests.id"],
            name="fk_agent_seal_manifests_manifest",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_agent_seal_manifest_ordinal"),
    )
    op.create_table(
        "agent_seal_evidence",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("seal_id", _uuid(), nullable=False),
        sa.Column("evidence_id", _uuid(), nullable=False),
        sa.Column("gate", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "seal_id",
            "evidence_id",
            name="pk_agent_seal_evidence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "evidence_id",
            name="uq_agent_seal_evidence_consumed",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "seal_id"],
            ["agent_candidate_seals.tenant_id", "agent_candidate_seals.id"],
            ondelete="CASCADE",
            name="fk_agent_seal_evidence_seal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "evidence_id"],
            ["agent_validation_evidence.tenant_id", "agent_validation_evidence.id"],
            name="fk_agent_seal_evidence_evidence",
        ),
        sa.CheckConstraint(
            "gate IN ('geometry', 'visual', 'dfm')",
            name="ck_agent_seal_evidence_gate",
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
        "GRANT SELECT, INSERT ON agent_seal_manifests, agent_seal_evidence "
        "TO cad_agent_runtime, cad_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "agent_seal_manifests, agent_seal_evidence TO cad_agent_migrator"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON artifact_uploads, change_sets, "
        "project_branches TO cad_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT ON artifacts, project_revisions TO cad_agent_worker"
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
        op.execute(f"DROP FUNCTION IF EXISTS reject_{table}_mutation()")
    op.drop_column("agent_candidate_seals", "selection")
