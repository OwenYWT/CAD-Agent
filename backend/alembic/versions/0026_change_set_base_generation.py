"""Freeze candidate base generations without guessing historical values."""
from alembic import op

revision = "0026_change_set_base_generation"
down_revision = "0025_semantic_checkpoints"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE change_sets ADD COLUMN base_state_version bigint CHECK(base_state_version >= 0)")
    # Only an operation's original persisted input proves its generation.
    # Candidates predating that evidence remain NULL and cannot be applied.
    op.execute("""UPDATE change_sets c SET base_state_version=o.base_state_version
        FROM cad_operations o WHERE o.id=c.source_workflow_run_id
          AND o.tenant_id=c.tenant_id AND o.document_id=c.branch_id
          AND o.base_revision_id=c.base_revision_id""")
    op.execute("""CREATE FUNCTION protect_change_set_base_generation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          IF NEW.base_state_version IS DISTINCT FROM OLD.base_state_version THEN
            RAISE EXCEPTION 'change set base generation is immutable';
          END IF;
          RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER change_set_base_generation_immutable BEFORE UPDATE ON change_sets
        FOR EACH ROW EXECUTE FUNCTION protect_change_set_base_generation()""")


def downgrade():
    op.execute("DROP TRIGGER change_set_base_generation_immutable ON change_sets")
    op.execute("DROP FUNCTION protect_change_set_base_generation()")
    op.execute("ALTER TABLE change_sets DROP COLUMN base_state_version")
