"""Append-only user intent with feature-local versions and ordered document events."""
from alembic import op

revision = "0017_feature_annotations"
down_revision = "0016_workflow_dispatch"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE feature_annotations (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, document_id uuid NOT NULL,
        feature_id uuid NOT NULL, kernel_name text NOT NULL,
        revision_id uuid NOT NULL, principal_id uuid NOT NULL,
        version integer NOT NULL CHECK(version>0), event_sequence bigint NOT NULL,
        role text CHECK(length(role)<=120), intent text CHECK(length(intent)<=2000),
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(document_id,feature_id,version),
        FOREIGN KEY(tenant_id,document_id) REFERENCES cloud_documents(tenant_id,id),
        FOREIGN KEY(tenant_id,revision_id) REFERENCES project_revisions(tenant_id,id),
        FOREIGN KEY(tenant_id,principal_id) REFERENCES principals(tenant_id,id)
    )""")
    op.execute("ALTER TABLE feature_annotations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE feature_annotations FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY feature_annotations_tenant ON feature_annotations
        USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
        WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON feature_annotations TO cad_agent_runtime,cad_agent_worker,cad_agent_migrator")
    op.execute("""CREATE TRIGGER feature_annotations_immutable BEFORE UPDATE OR DELETE ON feature_annotations
        FOR EACH ROW EXECUTE FUNCTION reject_document_evidence_mutation()""")
    op.execute("""CREATE FUNCTION protect_cad_operation_input() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF ROW(NEW.id,NEW.tenant_id,NEW.document_id,NEW.actor_principal_id,NEW.base_revision_id,
               NEW.base_state_version,NEW.action,NEW.arguments,NEW.idempotency_key,NEW.request_hash)
           IS DISTINCT FROM ROW(OLD.id,OLD.tenant_id,OLD.document_id,OLD.actor_principal_id,OLD.base_revision_id,
               OLD.base_state_version,OLD.action,OLD.arguments,OLD.idempotency_key,OLD.request_hash) THEN
            RAISE EXCEPTION 'CAD operation input is immutable';
        END IF; RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER cad_operation_input_immutable BEFORE UPDATE ON cad_operations FOR EACH ROW EXECUTE FUNCTION protect_cad_operation_input()")


def downgrade():
    op.execute("DROP TRIGGER cad_operation_input_immutable ON cad_operations")
    op.execute("DROP FUNCTION protect_cad_operation_input()")
    op.execute("DROP TABLE feature_annotations")
