"""Account-attributed provider calls and a narrowly scoped monitoring reader."""
from alembic import op

revision = "0028_llm_monitoring"
down_revision = "0027_document_scene_tasks"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE llm_calls (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, principal_id uuid NOT NULL,
        workflow_run_id uuid, activity_type text, activity_attempt integer,
        provider text NOT NULL, model text NOT NULL, request_hash text NOT NULL,
        provider_response_id text, response_hash text, finish_reason text,
        status text NOT NULL CHECK(status IN ('started','succeeded','truncated','failed','cancelled')),
        attempt integer NOT NULL CHECK(attempt > 0),
        started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at timestamptz, duration_ms bigint CHECK(duration_ms >= 0),
        prompt_tokens bigint CHECK(prompt_tokens >= 0),
        completion_tokens bigint CHECK(completion_tokens >= 0),
        total_tokens bigint CHECK(total_tokens >= 0),
        reasoning_tokens bigint CHECK(reasoning_tokens >= 0),
        error_code text, http_status integer,
        FOREIGN KEY(tenant_id, principal_id) REFERENCES principals(tenant_id,id),
        FOREIGN KEY(tenant_id, workflow_run_id) REFERENCES workflow_runs(tenant_id,id)
    )""")
    op.execute("CREATE INDEX llm_calls_started ON llm_calls(started_at DESC)")
    op.execute("CREATE INDEX llm_calls_account ON llm_calls(tenant_id,principal_id,started_at DESC)")
    op.execute("CREATE INDEX llm_calls_workflow ON llm_calls(workflow_run_id)")
    op.execute("ALTER TABLE llm_calls ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm_calls FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY llm_calls_tenant ON llm_calls
        USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
        WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT,UPDATE ON llm_calls TO cad_agent_runtime,cad_agent_worker")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='cad_agent_monitor') THEN
            CREATE ROLE cad_agent_monitor NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        END IF;
        EXECUTE format('GRANT cad_agent_monitor TO %I',current_user);
    END $$""")
    op.execute("GRANT USAGE ON SCHEMA public TO cad_agent_monitor")
    op.execute("GRANT SELECT ON llm_calls TO cad_agent_monitor")
    op.execute("GRANT SELECT(id,tenant_id,kind,external_subject,display_name) ON principals TO cad_agent_monitor")
    op.execute("""GRANT SELECT(id,tenant_id,requested_by_principal_id,kind,status,error_code,
        created_at,started_at,updated_at,completed_at) ON workflow_runs TO cad_agent_monitor""")
    for table in ("principals", "workflow_runs", "llm_calls"):
        op.execute(f"CREATE POLICY {table}_monitor_read ON {table} FOR SELECT TO cad_agent_monitor USING(true)")


def downgrade():
    for table in ("principals", "workflow_runs", "llm_calls"):
        op.execute(f"DROP POLICY {table}_monitor_read ON {table}")
    op.execute("REVOKE ALL PRIVILEGES ON principals,workflow_runs FROM cad_agent_monitor")
    op.execute("REVOKE SELECT(id,tenant_id,kind,external_subject,display_name) ON principals FROM cad_agent_monitor")
    op.execute("""REVOKE SELECT(id,tenant_id,requested_by_principal_id,kind,status,error_code,
        created_at,started_at,updated_at,completed_at) ON workflow_runs FROM cad_agent_monitor""")
    op.drop_table("llm_calls")
