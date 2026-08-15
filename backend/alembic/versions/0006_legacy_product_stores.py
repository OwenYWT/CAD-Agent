"""Tenant-safe replacements for every legacy product store.

Revision ID: 0006_legacy_product_stores
Revises: 0005_change_set_review
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_legacy_product_stores"
down_revision = "0005_change_set_review"
branch_labels = None
depends_on = None


TENANT_TABLES = (
    "auth_users",
    "auth_sessions",
    "auth_verification_codes",
    "auth_invite_codes",
    "workspace_sessions",
    "workspace_panels",
    "workspace_messages",
    "legacy_snapshot_mappings",
    "project_files",
    "dfm_rule_sets",
    "dfm_rules",
    "knowledge_nodes",
    "knowledge_edges",
    "product_feedback",
    "connector_links",
    "connector_tokens",
    "connector_oauth_states",
    "connector_records",
    "connector_audit_records",
    "connector_artifacts",
    "capability_artifacts",
    "legacy_quarantine_records",
    "legacy_import_runs",
    "legacy_import_mappings",
)

AUTH_TABLES = (
    "auth_users",
    "auth_sessions",
    "auth_verification_codes",
    "auth_invite_codes",
)

IMMUTABLE_TABLES = (
    "project_files",
    "connector_audit_records",
    "connector_artifacts",
    "capability_artifacts",
    "legacy_quarantine_records",
    "legacy_import_mappings",
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
    op.execute(
        """
        DO $$
        DECLARE migration_owner text := current_user;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname='cad_agent_auth'
            ) THEN
                CREATE ROLE cad_agent_auth
                    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOINHERIT NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname='cad_agent_migrator'
            ) THEN
                CREATE ROLE cad_agent_migrator
                    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOINHERIT NOBYPASSRLS;
            END IF;
            EXECUTE format('GRANT cad_agent_auth TO %I', migration_owner);
            EXECUTE format('GRANT cad_agent_migrator TO %I', migration_owner);
        END
        $$
        """
    )
    op.drop_constraint(
        "ck_project_revisions_kind",
        "project_revisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_project_revisions_kind",
        "project_revisions",
        "kind IN ('initial', 'candidate', 'rollback', 'imported')",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_project_revision_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF current_setting('app.legacy_import_rollback', true) = 'on'
               AND OLD.kind IN ('initial', 'imported') THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'project_revisions are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_task_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF current_setting('app.legacy_import_rollback', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'task_events are append-only';
        END;
        $$
        """
    )

    op.create_table(
        "auth_users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("phone_lookup_hash", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("registered_via", sa.Text(), nullable=False),
        _created_at(),
        sa.Column(
            "last_login_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_auth_users_tenant_id"),
        sa.UniqueConstraint("phone_lookup_hash", name="uq_auth_users_phone_lookup"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            ondelete="CASCADE",
            name="fk_auth_users_principal",
        ),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["auth_users.tenant_id", "auth_users.id"],
            ondelete="CASCADE",
            name="fk_auth_sessions_user",
        ),
    )
    op.create_table(
        "auth_verification_codes",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("phone_lookup_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        _created_at(),
        sa.CheckConstraint(
            "purpose IN ('register', 'login', 'reset_password')",
            name="ck_auth_verification_purpose",
        ),
    )
    op.create_table(
        "auth_invite_codes",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        _created_at(),
        sa.PrimaryKeyConstraint("tenant_id", "code", name="pk_auth_invite_codes"),
        sa.UniqueConstraint("code", name="uq_auth_invite_codes_code"),
        sa.CheckConstraint(
            "max_uses >= 1 AND used_count >= 0 AND used_count <= max_uses",
            name="ck_auth_invite_usage",
        ),
    )

    op.create_table(
        "workspace_sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("created_by_principal_id", _uuid(), nullable=False),
        sa.Column("legacy_user_id", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_workspace_sessions"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            name="uq_workspace_sessions_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
            name="fk_workspace_sessions_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_workspace_sessions_creator",
        ),
    )
    op.create_table(
        "workspace_panels",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("current_code", sa.Text()),
        sa.Column("current_params", _jsonb()),
        _created_at(),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_workspace_panels"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["workspace_sessions.tenant_id", "workspace_sessions.id"],
            ondelete="CASCADE",
            name="fk_workspace_panels_session",
        ),
    )
    op.create_table(
        "workspace_messages",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("panel_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("result", _jsonb()),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "panel_id"],
            ["workspace_panels.tenant_id", "workspace_panels.id"],
            ondelete="CASCADE",
            name="fk_workspace_messages_panel",
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="ck_workspace_messages_role",
        ),
    )
    op.create_table(
        "legacy_snapshot_mappings",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("legacy_snapshot_id", sa.Text(), nullable=False),
        sa.Column("panel_id", sa.Text(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("revision_id", _uuid(), nullable=False),
        sa.Column("legacy_version", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "legacy_snapshot_id",
            name="pk_legacy_snapshot_mappings",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "panel_id",
            "legacy_version",
            name="uq_legacy_snapshot_panel_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "panel_id"],
            ["workspace_panels.tenant_id", "workspace_panels.id"],
            ondelete="CASCADE",
            name="fk_legacy_snapshot_panel",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "revision_id"],
            [
                "project_revisions.tenant_id",
                "project_revisions.project_id",
                "project_revisions.id",
            ],
            name="fk_legacy_snapshot_revision",
        ),
        sa.CheckConstraint(
            "legacy_version >= 1",
            name="ck_legacy_snapshot_version",
        ),
    )
    op.create_table(
        "project_files",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("revision_id", _uuid()),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("object_key", name="uq_project_files_object_key"),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            "filename",
            name="uq_project_files_request_filename",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
            name="fk_project_files_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "revision_id"],
            [
                "project_revisions.tenant_id",
                "project_revisions.project_id",
                "project_revisions.id",
            ],
            name="fk_project_files_revision",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_project_files_size"),
    )

    op.create_table(
        "dfm_rule_sets",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("process", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False, server_default="1"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        _created_at(),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_dfm_rule_sets"),
    )
    op.create_table(
        "dfm_rules",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("rule_set_id", sa.Text(), nullable=False),
        sa.Column("process", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("check_type", sa.Text(), nullable=False, server_default="geometric"),
        sa.Column("threshold_min", sa.Float()),
        sa.Column("threshold_max", sa.Float()),
        sa.Column("unit", sa.Text(), nullable=False, server_default=""),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("suggestion_template", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_dfm_rules"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "rule_set_id"],
            ["dfm_rule_sets.tenant_id", "dfm_rule_sets.id"],
            ondelete="CASCADE",
            name="fk_dfm_rules_rule_set",
        ),
    )
    op.create_table(
        "knowledge_nodes",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("properties", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source", sa.Text(), nullable=False, server_default="user"),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "customer_id",
            "id",
            name="pk_knowledge_nodes",
        ),
    )
    op.create_table(
        "knowledge_edges",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("legacy_id", sa.BigInteger()),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("relationship", sa.Text(), nullable=False),
        sa.Column("properties", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source", sa.Text(), nullable=False, server_default="user"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id", "source_id"],
            ["knowledge_nodes.tenant_id", "knowledge_nodes.customer_id", "knowledge_nodes.id"],
            ondelete="CASCADE",
            name="fk_knowledge_edges_source",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id", "target_id"],
            ["knowledge_nodes.tenant_id", "knowledge_nodes.customer_id", "knowledge_nodes.id"],
            ondelete="CASCADE",
            name="fk_knowledge_edges_target",
        ),
    )

    op.create_table(
        "product_feedback",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid()),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("rating", sa.Text()),
        sa.Column("printed", sa.Text()),
        sa.Column("note", sa.Text()),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_product_feedback_principal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_product_feedback_project",
        ),
        sa.CheckConstraint(
            "rating IS NULL OR rating IN ('up', 'down')",
            name="ck_product_feedback_rating",
        ),
        sa.CheckConstraint(
            "printed IS NULL OR printed IN ('yes', 'no', 'not_yet')",
            name="ck_product_feedback_printed",
        ),
    )

    op.create_table(
        "connector_links",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid()),
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("element_id", sa.Text()),
        sa.Column("translation_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("external_url", sa.Text(), nullable=False),
        sa.Column("document_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_filename", sa.Text(), nullable=False, server_default=""),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("raw_response", _jsonb()),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_connector_links_principal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_connector_links_project",
        ),
    )
    op.create_table(
        "connector_tokens",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column("encryption_key_id", sa.Text(), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "principal_id",
            "connector",
            name="pk_connector_tokens",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            ondelete="CASCADE",
            name="fk_connector_tokens_principal",
        ),
    )
    op.create_table(
        "connector_oauth_states",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("encrypted_verifier", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        _created_at(),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "connector",
            "state",
            name="pk_connector_oauth_states",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            ondelete="CASCADE",
            name="fk_connector_oauth_states_principal",
        ),
    )
    op.create_table(
        "connector_records",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text()),
        sa.Column("payload", _jsonb(), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "connector",
            "record_type",
            "external_id",
            name="uq_connector_records_external",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_connector_records_principal",
        ),
    )
    op.create_table(
        "connector_audit_records",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_connector_audit_principal",
        ),
    )
    op.create_table(
        "connector_artifacts",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("object_key", name="uq_connector_artifacts_object_key"),
        sa.UniqueConstraint(
            "tenant_id",
            "connector",
            "request_id",
            "filename",
            name="uq_connector_artifacts_request_filename",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_connector_artifacts_principal",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_connector_artifacts_size"),
    )
    op.create_table(
        "capability_artifacts",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid()),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("object_key", name="uq_capability_artifacts_object_key"),
        sa.UniqueConstraint(
            "tenant_id",
            "scope",
            "request_id",
            "filename",
            name="uq_capability_artifacts_scope_request_filename",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_capability_artifacts_principal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_capability_artifacts_project",
        ),
        sa.CheckConstraint(
            "scope IN ('uploads', 'runs')",
            name="ck_capability_artifacts_scope",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_capability_artifacts_size"),
    )

    op.create_table(
        "legacy_quarantine_records",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            _jsonb(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id",
            "source_fingerprint",
            "source_reference",
            name="uq_legacy_quarantine_source",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            ondelete="CASCADE",
            name="fk_legacy_quarantine_principal",
        ),
    )

    op.create_table(
        "legacy_import_runs",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("report", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        _created_at(),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id",
            "source_fingerprint",
            name="uq_legacy_import_runs_source",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'rolled_back')",
            name="ck_legacy_import_runs_status",
        ),
    )
    op.create_table(
        "legacy_import_mappings",
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("source_store", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("target_table", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("import_run_id", _uuid(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "source_store",
            "entity_type",
            "source_id",
            name="pk_legacy_import_mappings",
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["legacy_import_runs.id"],
            ondelete="CASCADE",
            name="fk_legacy_import_mapping_run",
        ),
    )

    indexes = (
        ("ix_auth_sessions_user", "auth_sessions", ["tenant_id", "user_id", "expires_at"]),
        ("ix_auth_verification_lookup", "auth_verification_codes", ["phone_lookup_hash", "purpose", "expires_at"]),
        ("ix_workspace_sessions_updated", "workspace_sessions", ["tenant_id", "updated_at"]),
        ("ix_workspace_panels_session", "workspace_panels", ["tenant_id", "session_id", "created_at"]),
        ("ix_workspace_messages_panel", "workspace_messages", ["tenant_id", "panel_id", "id"]),
        ("ix_project_files_request", "project_files", ["tenant_id", "request_id"]),
        ("ix_dfm_rule_sets_process", "dfm_rule_sets", ["tenant_id", "process"]),
        ("ix_knowledge_nodes_type", "knowledge_nodes", ["tenant_id", "customer_id", "type"]),
        ("ix_knowledge_edges_source", "knowledge_edges", ["tenant_id", "customer_id", "source_id"]),
        ("ix_feedback_request", "product_feedback", ["tenant_id", "request_id", "created_at"]),
        ("ix_connector_links_request", "connector_links", ["tenant_id", "connector", "request_id", "updated_at"]),
        ("ix_connector_links_translation", "connector_links", ["tenant_id", "connector", "translation_id"]),
        ("ix_connector_records_type", "connector_records", ["tenant_id", "connector", "record_type", "updated_at"]),
        ("ix_connector_audit_request", "connector_audit_records", ["tenant_id", "connector", "request_id", "created_at"]),
        ("ix_capability_artifacts_request", "capability_artifacts", ["tenant_id", "scope", "request_id"]),
        ("ix_legacy_quarantine_fingerprint", "legacy_quarantine_records", ["tenant_id", "source_fingerprint"]),
    )
    for name, table, columns in indexes:
        op.create_index(name, table, columns)

    for table in TENANT_TABLES:
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

    for table in AUTH_TABLES:
        op.execute(
            f'CREATE POLICY "{table}_auth_service" ON "{table}" '
            "TO cad_agent_auth USING (true) WITH CHECK (true)"
        )
    op.execute(
        'CREATE POLICY "connector_oauth_states_auth_service" '
        'ON "connector_oauth_states" TO cad_agent_auth '
        "USING (true) WITH CHECK (true)"
    )

    for table in IMMUTABLE_TABLES:
        function_name = f"reject_{table}_mutation"
        trigger_name = f"{table}_immutable"
        op.execute(
            f"""
            CREATE FUNCTION {function_name}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF current_setting('app.legacy_import_rollback', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION '{table} rows are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {function_name}()
            """
        )

    runtime_rw = (
        "workspace_sessions",
        "workspace_panels",
        "workspace_messages",
        "legacy_snapshot_mappings",
        "dfm_rule_sets",
        "dfm_rules",
        "knowledge_nodes",
        "knowledge_edges",
        "product_feedback",
        "connector_links",
        "connector_tokens",
        "connector_oauth_states",
        "connector_records",
    )
    runtime_append = (
        "project_files",
        "connector_audit_records",
        "connector_artifacts",
        "capability_artifacts",
    )
    op.execute(
        "GRANT USAGE ON SCHEMA public TO cad_agent_auth, cad_agent_migrator"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        + ", ".join(runtime_rw)
        + " TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT ON "
        + ", ".join(runtime_append)
        + " TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        + ", ".join(AUTH_TABLES)
        + " TO cad_agent_auth"
    )
    op.execute(
        "GRANT SELECT, UPDATE ON connector_oauth_states TO cad_agent_auth"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        + ", ".join(TENANT_TABLES)
        + " TO cad_agent_migrator"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "tenants, principals, tenant_memberships, projects, "
        "project_memberships, workflow_runs, task_events, "
        "project_branches, project_revisions TO cad_agent_migrator"
    )
    op.execute(
        "GRANT SELECT ON "
        + ", ".join(
            (
                "workspace_sessions",
                "workspace_panels",
                "legacy_snapshot_mappings",
                "project_files",
                "dfm_rule_sets",
                "dfm_rules",
                "knowledge_nodes",
                "knowledge_edges",
                "connector_links",
                "connector_records",
                "connector_artifacts",
                "capability_artifacts",
            )
        )
        + " TO cad_agent_worker"
    )
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
        "TO cad_agent_runtime, cad_agent_auth, cad_agent_migrator"
    )


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}"')
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP FUNCTION IF EXISTS reject_{table}_mutation()")
    op.drop_constraint(
        "ck_project_revisions_kind",
        "project_revisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_project_revisions_kind",
        "project_revisions",
        "kind IN ('initial', 'candidate', 'rollback')",
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
        CREATE OR REPLACE FUNCTION reject_task_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'task_events are append-only';
        END;
        $$
        """
    )
