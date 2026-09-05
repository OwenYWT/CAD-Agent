"""Allow native BOM evidence to be bound into a candidate seal.

Revision ID: 0012_native_bom_seal_gate
Revises: 0011_freecad_fusion_acceptance
"""
from __future__ import annotations

from alembic import op


revision = "0012_native_bom_seal_gate"
down_revision = "0011_freecad_fusion_acceptance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_agent_seal_evidence_gate",
        "agent_seal_evidence",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_seal_evidence_gate",
        "agent_seal_evidence",
        "gate IN ('geometry', 'visual', 'dfm', 'bom')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_seal_evidence_gate",
        "agent_seal_evidence",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_seal_evidence_gate",
        "agent_seal_evidence",
        "gate IN ('geometry', 'visual', 'dfm')",
    )
