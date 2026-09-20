"""Persistent model jobs: worker leases are not generation deadlines."""
from alembic import op

revision = "0029_model_jobs"
down_revision = "0028_llm_monitoring"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE model_jobs (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, principal_id uuid NOT NULL,
        workflow_run_id uuid NOT NULL, operation text NOT NULL,
        payload jsonb NOT NULL, payload_hash text NOT NULL,
        status text NOT NULL DEFAULT 'queued' CHECK(status IN
            ('queued','running','succeeded','failed','cancelled')),
        generation integer NOT NULL DEFAULT 0, lease_until timestamptz,
        cancel_requested boolean NOT NULL DEFAULT false,
        result jsonb, error jsonb,
        created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
        FOREIGN KEY(tenant_id,principal_id) REFERENCES principals(tenant_id,id),
        FOREIGN KEY(tenant_id,workflow_run_id) REFERENCES workflow_runs(tenant_id,id)
    )""")
    op.execute("CREATE INDEX model_jobs_queue ON model_jobs(status,lease_until,created_at)")
    op.execute("CREATE INDEX model_jobs_run ON model_jobs(workflow_run_id)")
    op.execute("ALTER TABLE model_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE model_jobs FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY model_jobs_tenant ON model_jobs
        USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
        WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT,UPDATE ON model_jobs TO cad_agent_runtime,cad_agent_worker")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='cad_agent_model_dispatch') THEN
            CREATE ROLE cad_agent_model_dispatch NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        END IF;
        EXECUTE format('GRANT cad_agent_model_dispatch TO %I',current_user);
    END $$""")
    op.execute("GRANT USAGE ON SCHEMA public TO cad_agent_model_dispatch")
    op.execute("GRANT SELECT,UPDATE ON model_jobs TO cad_agent_model_dispatch")
    op.execute("CREATE POLICY model_jobs_dispatch ON model_jobs TO cad_agent_model_dispatch USING(true) WITH CHECK(true)")


def downgrade():
    op.drop_table('model_jobs')
