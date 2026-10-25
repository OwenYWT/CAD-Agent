"""Versioned plans and tenant-owned customer balances, separate from provider costs."""
from alembic import op


revision = "0033_tenant_billing_foundation"
down_revision = "0032_provider_receipt_recovery"
branch_labels = None
depends_on = None

BASIC_PLAN = "8ffecfb1-41a1-49a2-a210-103300000001"
TENANT_TABLES = (
    "tenant_subscriptions", "tenant_wallet_accounts",
    "tenant_wallet_entries", "tenant_billing_events",
)


def upgrade():
    op.execute("""DO $$ DECLARE migration_owner text := current_user; BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cad_agent_billing_admin') THEN
            CREATE ROLE cad_agent_billing_admin
                NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        END IF;
        EXECUTE format('GRANT cad_agent_billing_admin TO %I', migration_owner);
    END $$""")
    op.execute("GRANT USAGE ON SCHEMA public TO cad_agent_billing_admin")
    op.execute("""CREATE TABLE billing_plan_versions (
        id uuid PRIMARY KEY, code text NOT NULL CHECK(code IN ('basic','pro','team')),
        version integer NOT NULL CHECK(version>0), name text NOT NULL,
        published boolean NOT NULL DEFAULT false,
        monthly_price_minor bigint CHECK(monthly_price_minor BETWEEN 0 AND 1000000000000),
        currency text NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'),
        monthly_provider_requests integer CHECK(monthly_provider_requests>0),
        created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(code,version)
    )""")
    # No price, paid entitlement or payment success is invented. The initial
    # basic version preserves the existing provider limits; other tiers are drafts.
    op.execute(f"""INSERT INTO billing_plan_versions
        (id,code,version,name,published,created_by) VALUES
        ('{BASIC_PLAN}','basic',1,'基础版',true,'migration:0033'),
        ('8ffecfb1-41a1-49a2-a210-103300000002','pro',1,'专业版',false,'migration:0033'),
        ('8ffecfb1-41a1-49a2-a210-103300000003','team',1,'团队版',false,'migration:0033')""")
    op.execute(f"""CREATE TABLE tenant_subscriptions (
        tenant_id uuid PRIMARY KEY REFERENCES tenants(id),
        plan_version_id uuid NOT NULL DEFAULT '{BASIC_PLAN}' REFERENCES billing_plan_versions(id),
        status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
        expires_at timestamptz, revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE tenant_wallet_accounts (
        tenant_id uuid PRIMARY KEY REFERENCES tenants(id),
        currency text NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'),
        balance_minor bigint NOT NULL DEFAULT 0 CHECK(balance_minor BETWEEN 0 AND 1000000000000),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE tenant_wallet_entries (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenant_wallet_accounts(tenant_id),
        kind text NOT NULL CHECK(kind='admin_adjustment'),
        amount_minor bigint NOT NULL CHECK(amount_minor<>0 AND abs(amount_minor)<=1000000000000),
        balance_after_minor bigint NOT NULL CHECK(balance_after_minor BETWEEN 0 AND 1000000000000),
        currency text NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'),
        idempotency_key text NOT NULL, reference text NOT NULL, reason text NOT NULL,
        actor_subject text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(tenant_id,idempotency_key)
    )""")
    op.execute("""CREATE TABLE tenant_billing_events (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
        idempotency_key text NOT NULL, operation text NOT NULL,
        request jsonb NOT NULL, result jsonb NOT NULL, actor_subject text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,idempotency_key)
    )""")
    # Migration backfill is deliberately before RLS, and never changes any
    # existing provider policy, usage, reservation or customer model.
    for table in ("tenant_subscriptions", "tenant_wallet_accounts"):
        op.execute(f"""INSERT INTO {table}(tenant_id)
            SELECT id FROM tenants WHERE kind IN ('personal','organization')
            ON CONFLICT(tenant_id) DO NOTHING""")
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON {table}
            USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
            WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT ON {table} TO cad_agent_runtime,cad_agent_worker,cad_agent_billing_admin")
    op.execute("""GRANT SELECT ON billing_plan_versions TO
        cad_agent_runtime,cad_agent_worker,cad_agent_billing_admin""")
    op.execute("GRANT INSERT ON billing_plan_versions TO cad_agent_billing_admin")
    for table in ("tenant_subscriptions", "tenant_wallet_accounts"):
        # Normal application code can only create the default zero-balance/basic
        # account. It cannot self-upgrade a plan or credit a wallet via SQL grants.
        op.execute(f"GRANT INSERT(tenant_id) ON {table} TO cad_agent_runtime,cad_agent_worker,cad_agent_billing_admin")
        op.execute(f"GRANT UPDATE ON {table} TO cad_agent_billing_admin")
    op.execute("GRANT SELECT ON tenants,principals,tenant_memberships TO cad_agent_billing_admin")
    for table in ("tenant_wallet_entries", "tenant_billing_events"):
        op.execute(f"GRANT INSERT ON {table} TO cad_agent_billing_admin")
    op.execute("""CREATE FUNCTION reject_billing_history_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'billing history is append-only';
        END $$""")
    for table in ("billing_plan_versions", "tenant_wallet_entries", "tenant_billing_events"):
        op.execute(f"""CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_billing_history_mutation()""")
    op.execute("""CREATE INDEX provider_budget_reservations_tenant_created
        ON provider_budget_reservations(tenant_id,created_at)""")


def downgrade():
    # Never mistake rows hidden by forced RLS for an empty customer ledger.
    # A migration identity unable to inspect all tenants must fail closed.
    op.execute("SET LOCAL row_security = off")
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM tenant_wallet_entries)
            OR EXISTS(SELECT 1 FROM tenant_billing_events)
            OR EXISTS(SELECT 1 FROM billing_plan_versions WHERE version>1)
        THEN RAISE EXCEPTION 'Customer billing records must be preserved; downgrade is blocked';
        END IF;
    END $$""")
    op.execute("DROP INDEX provider_budget_reservations_tenant_created")
    for table in ("tenant_billing_events", "tenant_wallet_entries", "tenant_wallet_accounts", "tenant_subscriptions", "billing_plan_versions"):
        op.drop_table(table)
    op.execute("DROP FUNCTION reject_billing_history_mutation()")
