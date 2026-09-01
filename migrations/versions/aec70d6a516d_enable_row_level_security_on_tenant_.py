"""enable row level security on tenant scoped tables

Revision ID: aec70d6a516d
Revises: 42d02c887049
Create Date: 2026-09-01 16:52:10.000000

Postgres enforces tenant isolation from here on. The application also filters
by tenant_id; these policies are the second, independent layer that holds even
if an application-level filter is forgotten.

`tenants` is deliberately excluded: the /api/tenants routes are an
administrative surface that must list every tenant.

Hand-written: Alembic cannot autogenerate policies.
"""

from collections.abc import Sequence

from alembic import op

revision: str = 'aec70d6a516d'
down_revision: str | None = '42d02c887049'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("workspaces", "documents")
POLICY = "tenant_isolation"

# current_setting(..., true) returns NULL when unset rather than raising, so a
# session that never set the tenant matches nothing. Fail-closed: forgetting to
# scope a session shows you no rows, never everyone's rows.
#
# NULLIF guards the empty-string case, which would otherwise fail the ::uuid cast.
PREDICATE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # USING filters what can be read; WITH CHECK stops rows being written
        # into another tenant. Both are needed.
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} "
            f"USING ({PREDICATE}) "
            f"WITH CHECK ({PREDICATE})"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
