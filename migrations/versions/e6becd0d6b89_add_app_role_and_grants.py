"""add app role and grants

Revision ID: e6becd0d6b89
Revises: 43582f1dcb78
Create Date: 2026-09-01 02:21:54.251136

Creates the non-superuser role the API connects as at runtime. Postgres exempts
superusers and table owners from row-level security, so the runtime role must be
unprivileged for the Step 14 RLS policies to have any effect.

Hand-written: Alembic cannot autogenerate roles or grants.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import get_settings

revision: str = 'e6becd0d6b89'
down_revision: str | None = '43582f1dcb78'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"


def _quote(conn, fn: str, value: str) -> str:
    """Let Postgres escape identifiers/literals instead of doing it ourselves.

    CREATE ROLE is a utility statement, so Postgres rejects bind parameters in it.
    Asking the server to quote the value first keeps the interpolation safe.
    """
    return conn.execute(sa.text(f"SELECT {fn}(:v)"), {"v": value}).scalar()


def _role_exists(conn, role: str) -> bool:
    return (
        conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
        is not None
    )


def upgrade() -> None:
    settings = get_settings()
    role, password = settings.app_db_user, settings.app_db_password

    if password in ("", "change-me"):
        raise RuntimeError(
            "APP_DB_PASSWORD is unset or still the placeholder. "
            "Set a real value in .env before running this migration."
        )

    conn = op.get_bind()
    role_ident = _quote(conn, "quote_ident", role)
    pw_literal = _quote(conn, "quote_literal", password)
    db_ident = _quote(
        conn, "quote_ident", conn.execute(sa.text("SELECT current_database()")).scalar()
    )

    if _role_exists(conn, role):
        conn.execute(sa.text(f"ALTER ROLE {role_ident} WITH LOGIN PASSWORD {pw_literal}"))
    else:
        conn.execute(sa.text(f"CREATE ROLE {role_ident} LOGIN PASSWORD {pw_literal}"))

    # Baseline access.
    conn.execute(sa.text(f"GRANT CONNECT ON DATABASE {db_ident} TO {role_ident}"))
    conn.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {role_ident}"))

    # Step 12's existing tables.
    conn.execute(
        sa.text(f"GRANT {TABLE_PRIVILEGES} ON ALL TABLES IN SCHEMA public TO {role_ident}")
    )
    conn.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role_ident}"))

    # Future tables, so Steps 15-20 need no extra grants.
    conn.execute(
        sa.text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT {TABLE_PRIVILEGES} ON TABLES TO {role_ident}"
        )
    )
    conn.execute(
        sa.text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {role_ident}"
        )
    )


def downgrade() -> None:
    role = get_settings().app_db_user
    conn = op.get_bind()

    if not _role_exists(conn, role):
        return

    role_ident = _quote(conn, "quote_ident", role)
    db_ident = _quote(
        conn, "quote_ident", conn.execute(sa.text("SELECT current_database()")).scalar()
    )

    conn.execute(
        sa.text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"REVOKE {TABLE_PRIVILEGES} ON TABLES FROM {role_ident}"
        )
    )
    conn.execute(
        sa.text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"REVOKE USAGE, SELECT ON SEQUENCES FROM {role_ident}"
        )
    )
    conn.execute(sa.text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_ident}"))
    conn.execute(sa.text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role_ident}"))
    conn.execute(sa.text(f"REVOKE ALL ON SCHEMA public FROM {role_ident}"))
    conn.execute(sa.text(f"REVOKE ALL ON DATABASE {db_ident} FROM {role_ident}"))
    conn.execute(sa.text(f"DROP OWNED BY {role_ident}"))
    conn.execute(sa.text(f"DROP ROLE {role_ident}"))
