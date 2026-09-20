"""Preserve JSON number representation across model job handoffs."""
from alembic import op

revision = "0030_model_job_json_numbers"
down_revision = "0029_model_jobs"
branch_labels = None
depends_on = None


def upgrade():
    # These columns are opaque execution envelopes, never JSONB-indexed or
    # compared in SQL. json preserves floating-point exponent/decimal tokens;
    # jsonb expands them and Python then reads some floats as huge integers.
    for column in ("payload", "result", "error"):
        op.execute(f"ALTER TABLE model_jobs ALTER COLUMN {column} TYPE json USING {column}::json")


def downgrade():
    for column in ("payload", "result", "error"):
        op.execute(f"ALTER TABLE model_jobs ALTER COLUMN {column} TYPE jsonb USING {column}::jsonb")
