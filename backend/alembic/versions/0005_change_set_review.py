"""Separate Change Set approval, commit, and rollback state.

Revision ID: 0005_change_set_review
Revises: 0004_artifacts
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_change_set_review"
down_revision = "0004_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_change_sets_status",
        "change_sets",
        type_="check",
    )
    op.add_column(
        "change_sets",
        sa.Column(
            "reviewed_by_principal_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "change_sets",
        sa.Column("review_note", sa.Text(), nullable=True),
    )
    op.add_column(
        "change_sets",
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "change_sets",
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "change_sets",
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_change_sets_reviewer",
        "change_sets",
        "principals",
        ["tenant_id", "reviewed_by_principal_id"],
        ["tenant_id", "id"],
    )
    op.create_check_constraint(
        "ck_change_sets_status",
        "change_sets",
        "status IN ('pending_review', 'accepted', 'committed', 'rejected', "
        "'changes_requested', 'superseded', 'rolled_back')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_change_sets_status",
        "change_sets",
        type_="check",
    )
    op.drop_constraint(
        "fk_change_sets_reviewer",
        "change_sets",
        type_="foreignkey",
    )
    for column in (
        "rolled_back_at",
        "committed_at",
        "accepted_at",
        "review_note",
        "reviewed_by_principal_id",
    ):
        op.drop_column("change_sets", column)
    op.create_check_constraint(
        "ck_change_sets_status",
        "change_sets",
        "status IN ('pending_review', 'accepted', 'rejected', "
        "'changes_requested', 'superseded')",
    )
