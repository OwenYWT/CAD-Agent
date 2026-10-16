"""Durable receipt outbox, independent of the model job's terminal status."""
from alembic import op

revision = '0032_provider_receipt_recovery'
down_revision = '0031_provider_budgets'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''ALTER TABLE provider_budget_reservations
        ADD COLUMN settlement_next_attempt_at timestamptz,
        ADD COLUMN settlement_attempts integer NOT NULL DEFAULT 0 CHECK(settlement_attempts>=0),
        ADD COLUMN settlement_last_error text,
        ADD CONSTRAINT provider_settlement_has_receipt CHECK(settlement_next_attempt_at IS NULL OR
            (receipt IS NOT NULL AND response IS NOT NULL AND status IN ('dispatch_claimed','unknown')))''')
    op.execute('''CREATE INDEX provider_settlement_due ON provider_budget_reservations(settlement_next_attempt_at)
        WHERE settlement_next_attempt_at IS NOT NULL''')
    # The global dispatcher can lease queue identities, not read model replies
    # or modify money. Settlement still runs under the original tenant's RLS.
    op.execute('''GRANT SELECT(id,tenant_id,model_job_id,generation,status,settlement_next_attempt_at,settlement_attempts)
        ON provider_budget_reservations TO cad_agent_model_dispatch''')
    op.execute('''GRANT UPDATE(settlement_next_attempt_at,settlement_attempts)
        ON provider_budget_reservations TO cad_agent_model_dispatch''')
    op.execute('''CREATE POLICY provider_settlement_dispatch_read ON provider_budget_reservations
        FOR SELECT TO cad_agent_model_dispatch USING(true)''')
    op.execute('''CREATE POLICY provider_settlement_dispatch_lease ON provider_budget_reservations
        FOR UPDATE TO cad_agent_model_dispatch USING(true) WITH CHECK(true)''')


def downgrade():
    # Removing a live queue would lose the only automatic recovery path.
    op.execute('''DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM provider_budget_reservations WHERE settlement_next_attempt_at IS NOT NULL)
        THEN RAISE EXCEPTION 'Drain pending provider settlements before downgrading'; END IF;
        END $$''')
    op.execute('DROP POLICY provider_settlement_dispatch_read ON provider_budget_reservations')
    op.execute('DROP POLICY provider_settlement_dispatch_lease ON provider_budget_reservations')
    op.execute('''REVOKE SELECT(id,tenant_id,model_job_id,generation,status,settlement_next_attempt_at,settlement_attempts),
        UPDATE(settlement_next_attempt_at,settlement_attempts) ON provider_budget_reservations FROM cad_agent_model_dispatch''')
    op.execute('DROP INDEX provider_settlement_due')
    op.execute('''ALTER TABLE provider_budget_reservations DROP CONSTRAINT provider_settlement_has_receipt,
        DROP COLUMN settlement_next_attempt_at, DROP COLUMN settlement_attempts, DROP COLUMN settlement_last_error''')
