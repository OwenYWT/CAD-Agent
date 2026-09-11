"""Atomic Temporal dispatch intents and narrowly scoped background recovery."""
from alembic import op

revision = "0016_workflow_dispatch"
down_revision = "0015_document_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cad_agent_dispatcher') THEN
            CREATE ROLE cad_agent_dispatcher NOLOGIN NOSUPERUSER NOBYPASSRLS;
        END IF;
        EXECUTE format('GRANT cad_agent_dispatcher TO %I', current_user);
    END $$""")
    op.execute("""CREATE TABLE workflow_dispatches (
        workflow_id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        workflow_type text NOT NULL CHECK (workflow_type IN ('McadAgentWorkflowV2','McadDurableWorkflow','McadCheckWorkflow')),
        temporal_id text NOT NULL UNIQUE,
        task_queue text NOT NULL,
        payload jsonb NOT NULL,
        payload_hash text NOT NULL,
        status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','dispatching','dispatched')),
        attempts integer NOT NULL DEFAULT 0,
        lease_token uuid,
        lease_until timestamptz,
        next_attempt_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_error_code text,
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        dispatched_at timestamptz,
        FOREIGN KEY (tenant_id,workflow_id) REFERENCES workflow_runs(tenant_id,id),
        FOREIGN KEY (tenant_id,principal_id) REFERENCES principals(tenant_id,id)
    )""")
    op.execute("CREATE INDEX workflow_dispatch_pending ON workflow_dispatches(next_attempt_at, created_at) WHERE status <> 'dispatched'")
    op.execute("ALTER TABLE workflow_dispatches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflow_dispatches FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY workflow_dispatch_tenant ON workflow_dispatches
        TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator
        USING (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid)
        WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid)""")
    op.execute("CREATE POLICY workflow_dispatch_background ON workflow_dispatches TO cad_agent_dispatcher USING (true)")
    op.execute("GRANT USAGE ON SCHEMA public TO cad_agent_dispatcher")
    op.execute("GRANT SELECT,INSERT,UPDATE ON workflow_dispatches TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator")
    op.execute("GRANT SELECT,UPDATE ON workflow_dispatches TO cad_agent_dispatcher")
    op.execute("""CREATE FUNCTION protect_workflow_dispatch_input() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF ROW(NEW.workflow_id,NEW.tenant_id,NEW.principal_id,NEW.workflow_type,NEW.temporal_id,NEW.task_queue,NEW.payload,NEW.payload_hash)
           IS DISTINCT FROM ROW(OLD.workflow_id,OLD.tenant_id,OLD.principal_id,OLD.workflow_type,OLD.temporal_id,OLD.task_queue,OLD.payload,OLD.payload_hash) THEN
            RAISE EXCEPTION 'workflow dispatch input is immutable';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER workflow_dispatch_immutable BEFORE UPDATE ON workflow_dispatches FOR EACH ROW EXECUTE FUNCTION protect_workflow_dispatch_input()")


def downgrade():
    op.execute("DROP TABLE workflow_dispatches")
    op.execute("DROP FUNCTION protect_workflow_dispatch_input()")
