"""Tenant ownership, identity, project policy, audit, and usage.

Revision ID: 0001_tenants_and_identity
Revises:
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_tenants_and_identity"
down_revision = None
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "tenants",
    "principals",
    "tenant_memberships",
    "projects",
    "project_memberships",
    "audit_records",
    "usage_meter_entries",
)


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True)


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('personal', 'organization', 'service', "
            "'local_development', 'quarantine')",
            name="ck_tenants_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'quarantined')",
            name="ck_tenants_status",
        ),
    )
    op.create_table(
        "principals",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            _uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_subject", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("api_key_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_principals_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "kind",
            "external_subject",
            name="uq_principals_tenant_kind_external_subject",
        ),
        sa.UniqueConstraint(
            "api_key_fingerprint",
            name="uq_principals_api_key_fingerprint",
        ),
        sa.CheckConstraint(
            "kind IN ('user', 'service', 'local_anonymous', 'quarantine')",
            name="ck_principals_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'quarantined')",
            name="ck_principals_status",
        ),
        sa.CheckConstraint(
            "(kind = 'service' AND api_key_fingerprint IS NOT NULL) "
            "OR (kind <> 'service' AND api_key_fingerprint IS NULL)",
            name="ck_principals_service_fingerprint",
        ),
    )
    op.create_table(
        "tenant_memberships",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "principal_id",
            name="pk_tenant_memberships",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            ondelete="CASCADE",
            name="fk_tenant_memberships_principal",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'service')",
            name="ck_tenant_memberships_role",
        ),
    )
    op.create_table(
        "projects",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            _uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by_principal_id", _uuid(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_projects_tenant_slug"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_projects_creator",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_projects_status",
        ),
    )
    op.create_table(
        "project_memberships",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "project_id",
            "principal_id",
            name="pk_project_memberships",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
            name="fk_project_memberships_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            ondelete="CASCADE",
            name="fk_project_memberships_principal",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'editor', 'viewer')",
            name="ck_project_memberships_role",
        ),
    )
    op.create_table(
        "audit_records",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("actor_principal_id", _uuid(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_audit_records_actor",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_audit_records_project",
        ),
    )
    op.create_table(
        "usage_meter_entries",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("billing_dimension", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_usage_meter_entries_principal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_usage_meter_entries_project",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_usage_meter_entries_idempotency",
        ),
        sa.CheckConstraint("quantity >= 0", name="ck_usage_quantity_nonnegative"),
    )
    op.create_index(
        "ix_audit_records_tenant_occurred",
        "audit_records",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_usage_meter_entries_tenant_occurred",
        "usage_meter_entries",
        ["tenant_id", "occurred_at"],
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_audit_record_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_records are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_records_append_only
        BEFORE UPDATE OR DELETE ON audit_records
        FOR EACH ROW EXECUTE FUNCTION reject_audit_record_mutation()
        """
    )

    for table in TENANT_TABLES:
        tenant_expression = (
            "id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
            if table == "tenants"
            else "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
        )
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            f"USING ({tenant_expression}) WITH CHECK ({tenant_expression})"
        )

    op.execute(
        """
        DO $$
        DECLARE migration_owner text := current_user;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cad_agent_runtime') THEN
                CREATE ROLE cad_agent_runtime
                    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cad_agent_worker') THEN
                CREATE ROLE cad_agent_worker
                    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
            END IF;
            EXECUTE format('GRANT cad_agent_runtime TO %I', migration_owner);
            EXECUTE format('GRANT cad_agent_worker TO %I', migration_owner);
        END
        $$
        """
    )
    op.execute(
        "GRANT USAGE ON SCHEMA public TO cad_agent_runtime, cad_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON tenants, principals, tenant_memberships, projects, project_memberships "
        "TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT ON audit_records, usage_meter_entries "
        "TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT ON tenants, principals, tenant_memberships, projects, "
        "project_memberships TO cad_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT ON audit_records, usage_meter_entries "
        "TO cad_agent_worker"
    )


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS reject_audit_record_mutation()")
