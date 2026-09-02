"""Row-level security tests.

These deliberately bypass the service layer and issue raw SQL with no WHERE
clause. If application-level filtering were the only protection, every one of
these would fail.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.models import Document, Tenant, Workspace


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"),
        {"t": str(tenant_id)},
    )


def _seed(db: Session, tenant_name: str, workspace_name: str) -> tuple[Tenant, Workspace]:
    tenant = Tenant(name=tenant_name)
    db.add(tenant)
    db.flush()

    _scope(db, tenant.id)
    workspace = Workspace(tenant_id=tenant.id, name=workspace_name)
    db.add(workspace)
    db.flush()

    document = Document(
        workspace_id=workspace.id,
        tenant_id=tenant.id,
        title=f"{workspace_name} notes",
        content="x",
    )
    db.add(document)
    db.flush()
    return tenant, workspace


def _count(db: Session, table: str) -> int:
    """No WHERE clause. Whatever comes back is what Postgres is willing to show."""
    return db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_unscoped_session_sees_nothing(db_session: Session) -> None:
    _seed(db_session, "acme", "sales")

    _scope(db_session, "")  # simulate a session that never set its tenant
    assert _count(db_session, "workspaces") == 0
    assert _count(db_session, "documents") == 0


def test_scoped_session_sees_only_its_own_rows(db_session: Session) -> None:
    acme, _ = _seed(db_session, "acme", "sales")
    _seed(db_session, "globex", "marketing")

    _scope(db_session, acme.id)
    assert _count(db_session, "workspaces") == 1
    assert _count(db_session, "documents") == 1

    names = db_session.execute(text("SELECT name FROM workspaces")).scalars().all()
    assert names == ["sales"]


def test_orm_query_without_filter_is_still_isolated(db_session: Session) -> None:
    """Even a query the developer forgot to filter returns only the tenant's rows."""
    acme, _ = _seed(db_session, "acme", "sales")
    _seed(db_session, "globex", "marketing")

    _scope(db_session, acme.id)
    db_session.expire_all()
    all_workspaces = db_session.query(Workspace).all()  # no .filter() at all
    assert [w.name for w in all_workspaces] == ["sales"]


def test_cannot_write_a_row_into_another_tenant(db_session: Session) -> None:
    """WITH CHECK blocks inserts across the boundary, not just reads."""
    acme, _ = _seed(db_session, "acme", "sales")
    globex, _ = _seed(db_session, "globex", "marketing")

    _scope(db_session, acme.id)

    with pytest.raises(ProgrammingError) as exc:
        db_session.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, created_at, updated_at) "
                "VALUES (:i, :t, 'smuggled', now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(globex.id)},
        )
    assert "row-level security" in str(exc.value).lower()


def test_cannot_read_another_tenants_row_by_id(db_session: Session) -> None:
    """Knowing the exact primary key must not help."""
    _acme, acme_ws = _seed(db_session, "acme", "sales")
    globex, _ = _seed(db_session, "globex", "marketing")

    _scope(db_session, globex.id)
    found = db_session.execute(
        text("SELECT count(*) FROM workspaces WHERE id = :i"), {"i": str(acme_ws.id)}
    ).scalar_one()
    assert found == 0


def test_every_tenant_scoped_table_has_rls(db_session: Session) -> None:
    """Any table carrying tenant_id must have row-level security enabled.

    Derived rather than hardcoded on purpose: a migration in a later step that
    adds a tenant-scoped table but forgets its policy fails here, without anyone
    having to remember to update this list.
    """
    rows = db_session.execute(
        text(
            "SELECT c.relname AS name, c.relrowsecurity AS protected "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "  AND EXISTS ("
            "    SELECT 1 FROM information_schema.columns col "
            "    WHERE col.table_schema = 'public' "
            "      AND col.table_name = c.relname "
            "      AND col.column_name = 'tenant_id') "
            "ORDER BY c.relname"
        )
    ).all()

    assert rows, "expected at least one tenant-scoped table"
    unprotected = [row.name for row in rows if not row.protected]
    assert unprotected == [], f"tenant-scoped tables missing RLS: {unprotected}"


def test_app_user_cannot_bypass_rls(db_session: Session) -> None:
    """A superuser or BYPASSRLS role would make all of the above meaningless."""
    row = db_session.execute(
        text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
    ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False
