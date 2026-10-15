"""Atomic provider admission independent of lossy observation logs."""
from alembic import op

revision = '0031_provider_budgets'
down_revision = '0030_model_job_json_numbers'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE provider_budget_policies (
        workflow_run_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, policy jsonb NOT NULL,
        policy_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY(tenant_id,workflow_run_id) REFERENCES workflow_runs(tenant_id,id)
    )''')
    op.execute('''CREATE TABLE provider_budget_accounts (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), scope text NOT NULL,
        request_limit integer NOT NULL CHECK(request_limit>0), requests_used integer NOT NULL DEFAULT 0 CHECK(requests_used>=0),
        currency text, cost_limit numeric(30,12), settled numeric(30,12) NOT NULL DEFAULT 0,
        held numeric(30,12) NOT NULL DEFAULT 0 CHECK(held>=0), frozen boolean NOT NULL DEFAULT false,
        policy jsonb NOT NULL, UNIQUE(tenant_id,scope)
    )''')
    op.execute('''CREATE TABLE provider_budget_reservations (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, workflow_run_id uuid NOT NULL,
        model_job_id uuid NOT NULL REFERENCES model_jobs(id), generation integer NOT NULL,
        logical_call_id text NOT NULL, attempt integer NOT NULL, request_digest text NOT NULL,
        provider text NOT NULL, model text NOT NULL, policy jsonb NOT NULL,
        reserved_amount numeric(30,12) NOT NULL CHECK(reserved_amount>=0), actual_amount numeric(30,12),
        status text NOT NULL CHECK(status IN ('reserved','dispatch_claimed','settled','released_unsent','unknown','reconciled')),
        receipt jsonb, response json, created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY(tenant_id,workflow_run_id) REFERENCES workflow_runs(tenant_id,id),
        UNIQUE(model_job_id,generation,logical_call_id,attempt)
    )''')
    op.execute('''CREATE TABLE provider_budget_allocations (
        reservation_id uuid NOT NULL REFERENCES provider_budget_reservations(id),
        account_id uuid NOT NULL REFERENCES provider_budget_accounts(id), tenant_id uuid NOT NULL,
        PRIMARY KEY(reservation_id,account_id)
    )''')
    op.execute('''CREATE TABLE provider_budget_events (
        reservation_id uuid NOT NULL REFERENCES provider_budget_reservations(id), tenant_id uuid NOT NULL,
        event_type text NOT NULL, evidence jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(reservation_id,event_type)
    )''')
    for table in ('provider_budget_policies', 'provider_budget_accounts', 'provider_budget_reservations',
                  'provider_budget_allocations', 'provider_budget_events'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY {table}_tenant ON {table}
            USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
            WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)''')
        op.execute(f'GRANT SELECT,INSERT ON {table} TO cad_agent_runtime,cad_agent_worker')
    for table in ('provider_budget_accounts', 'provider_budget_reservations'):
        op.execute(f'GRANT UPDATE ON {table} TO cad_agent_runtime,cad_agent_worker')
    op.execute('CREATE INDEX provider_budget_pending ON provider_budget_reservations(status,updated_at)')


def downgrade():
    for table in ('provider_budget_events', 'provider_budget_allocations', 'provider_budget_reservations',
                  'provider_budget_accounts', 'provider_budget_policies'):
        op.drop_table(table)
