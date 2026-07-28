"""Durable workflows, fenced attempts, events, and outbox.

Revision ID: 0002_workflows_and_events
Revises: 0001_tenants_and_identity
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_workflows_and_events"
down_revision = "0001_tenants_and_identity"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "workflow_runs",
    "step_runs",
    "execution_attempts",
    "task_events",
    "outbox_messages",
)


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True)


def _jsonb() -> sa.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("requested_by_principal_id", _uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("request_payload", _jsonb(), nullable=False),
        sa.Column(
            "last_event_sequence",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_workflow_runs_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_workflow_runs_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_workflow_runs_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "requested_by_principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_workflow_runs_requester",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'planning', 'running', "
            "'waiting_confirmation', 'cancelling', 'succeeded', 'failed', "
            "'cancelled', 'timed_out')",
            name="ck_workflow_runs_status",
        ),
    )
    op.create_table(
        "step_runs",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_run_id",
            "id",
            name="uq_step_runs_tenant_workflow_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_run_id",
            "step_key",
            name="uq_step_runs_workflow_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_run_id",
            "step_index",
            name="uq_step_runs_workflow_index",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.id"],
            ondelete="CASCADE",
            name="fk_step_runs_workflow",
        ),
        sa.CheckConstraint("step_index >= 0", name="ck_step_runs_index"),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'succeeded', "
            "'failed', 'skipped', 'cancelled', 'timed_out')",
            name="ck_step_runs_status",
        ),
    )
    op.create_table(
        "execution_attempts",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("step_run_id", _uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("execution_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_payload", _jsonb(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token_hash", sa.String(length=64)),
        sa.Column("worker_id", sa.Text()),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("result_payload_hash", sa.String(length=64)),
        sa.Column("result_payload", _jsonb()),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_execution_attempts_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_execution_attempts_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "step_run_id",
            "attempt_number",
            name="uq_execution_attempts_step_number",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_run_id", "step_run_id"],
            [
                "step_runs.tenant_id",
                "step_runs.workflow_run_id",
                "step_runs.id",
            ],
            ondelete="CASCADE",
            name="fk_execution_attempts_step",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_execution_attempts_number",
        ),
        sa.CheckConstraint(
            "lease_generation >= 0",
            name="ck_execution_attempts_generation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'running', 'succeeded', "
            "'failed', 'cancelled', 'timed_out')",
            name="ck_execution_attempts_status",
        ),
    )
    op.create_table(
        "task_events",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_task_events_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_run_id",
            "sequence",
            name="uq_task_events_workflow_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.id"],
            ondelete="CASCADE",
            name="fk_task_events_workflow",
        ),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("tenant_id", _uuid(), nullable=False),
        sa.Column("workflow_run_id", _uuid(), nullable=False),
        sa.Column("event_id", _uuid(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("event_id", name="uq_outbox_messages_event"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.id"],
            ondelete="CASCADE",
            name="fk_outbox_messages_workflow",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "event_id"],
            ["task_events.tenant_id", "task_events.id"],
            ondelete="CASCADE",
            name="fk_outbox_messages_event",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed')",
            name="ck_outbox_messages_status",
        ),
    )
    op.create_index(
        "ix_workflow_runs_tenant_status_updated",
        "workflow_runs",
        ["tenant_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_execution_attempts_lease",
        "execution_attempts",
        ["tenant_id", "status", "leased_until"],
    )
    op.create_index(
        "ix_outbox_messages_delivery",
        "outbox_messages",
        ["tenant_id", "status", "available_at", "created_at"],
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
    op.execute(
        """
        CREATE TRIGGER task_events_append_only
        BEFORE UPDATE OR DELETE ON task_events
        FOR EACH ROW EXECUTE FUNCTION reject_task_event_mutation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_execution_attempt_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.workflow_run_id IS DISTINCT FROM OLD.workflow_run_id
               OR NEW.step_run_id IS DISTINCT FROM OLD.step_run_id
               OR NEW.attempt_number IS DISTINCT FROM OLD.attempt_number
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.execution_payload_hash IS DISTINCT FROM OLD.execution_payload_hash
               OR NEW.execution_payload IS DISTINCT FROM OLD.execution_payload THEN
                RAISE EXCEPTION 'execution attempt identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER execution_attempt_identity_immutable
        BEFORE UPDATE ON execution_attempts
        FOR EACH ROW EXECUTE FUNCTION protect_execution_attempt_identity()
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
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON workflow_runs, step_runs, execution_attempts TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT ON task_events TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON outbox_messages TO cad_agent_runtime"
    )
    op.execute(
        "GRANT SELECT ON workflow_runs, step_runs, execution_attempts "
        "TO cad_agent_worker"
    )
    op.execute(
        "GRANT UPDATE (last_event_sequence, updated_at) "
        "ON workflow_runs TO cad_agent_worker"
    )
    op.execute(
        "GRANT UPDATE (status, lease_generation, lease_token_hash, worker_id, "
        "leased_until, last_heartbeat_at, result_payload_hash, result_payload, "
        "error_code, error_message, started_at, updated_at, completed_at) "
        "ON execution_attempts TO cad_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT ON task_events, outbox_messages TO cad_agent_worker"
    )


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS protect_execution_attempt_identity()")
    op.execute("DROP FUNCTION IF EXISTS reject_task_event_mutation()")
