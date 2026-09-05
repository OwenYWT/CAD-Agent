"""Persist structured execution errors for FreeCAD fusion acceptance.

Revision ID: 0011_freecad_fusion_acceptance
Revises: 0010_agent_candidate_seal_links
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_freecad_fusion_acceptance"
down_revision = "0010_agent_candidate_seal_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("execution_attempts", "step_runs"):
        op.add_column(
            table,
            sa.Column(
                "error_details",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
    op.drop_constraint(
        "ck_agent_validation_evidence_gate",
        "agent_validation_evidence",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_validation_evidence_gate",
        "agent_validation_evidence",
        "gate IN ('artifact_integrity', 'geometry', 'visual', 'dfm', 'bom')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_validation_evidence_gate",
        "agent_validation_evidence",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_validation_evidence_gate",
        "agent_validation_evidence",
        "gate IN ('artifact_integrity', 'geometry', 'visual', 'dfm')",
    )
    for table in ("step_runs", "execution_attempts"):
        op.drop_column(table, "error_details")
