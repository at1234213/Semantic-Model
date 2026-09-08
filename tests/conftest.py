from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import engine, get_db
from app.main import app
from app.services import warehouse


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A session inside a transaction that is always rolled back.

    join_transaction_mode="create_savepoint" lets the routes call commit()
    without ending the outer transaction, so tests leave no rows behind.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True, scope="session")
def _dispose_warehouse_engines() -> Iterator[None]:
    """Warehouse engines are cached across tests and hold connection pools."""
    yield
    warehouse.dispose_engines()


@pytest.fixture(scope="session")
def warehouse_tables() -> Iterator[str]:
    """Real tables in a real schema, so execution tests exercise the genuine path.

    The data plane is a separate database in production. For tests it is a
    separate *schema* in the same container, reached through the same
    data_source machinery — read-only connection, statement timeout and all.
    Created with a writable connection because the warehouse one refuses writes,
    which is the behaviour under test.
    """
    from sqlalchemy import create_engine

    from app.core.config import get_settings

    engine = create_engine(
        get_settings().alembic_database_url, isolation_level="AUTOCOMMIT"
    )
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS warehouse CASCADE"))
        connection.execute(text("CREATE SCHEMA warehouse"))
        connection.execute(
            text(
                "CREATE TABLE warehouse.customers ("
                " id int PRIMARY KEY, country text, is_internal boolean, seats int)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE warehouse.orders ("
                " id int PRIMARY KEY, customer_id int, amount numeric, order_date date)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO warehouse.customers VALUES "
                "(1,'US',false,10),(2,'US',false,50),(3,'CA',false,5),(4,'US',true,999)"
            )
        )
        # Q2 2026 is "last quarter" relative to the tests' fixed today.
        # Order 4 belongs to an internal customer; order 5 falls outside the quarter.
        connection.execute(
            text(
                "INSERT INTO warehouse.orders VALUES "
                "(1,1,100.00,'2026-04-15'),(2,2,250.00,'2026-05-20'),"
                "(3,3,50.00,'2026-06-10'),(4,4,999.00,'2026-05-01'),"
                "(5,1,10.00,'2026-01-05')"
            )
        )
    yield "warehouse"
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS warehouse CASCADE"))
    engine.dispose()


@pytest.fixture
def make_tenant(db_session: Session):
    """Create a tenant and an API key for it, returning (tenant, auth headers).

    Isolation tests now use two genuinely separate credentials rather than two
    values of a header the caller chose, which is a stronger claim.
    """
    from app.models import Tenant
    from app.services import api_keys

    def _make(name: str, *, is_admin: bool = False):
        tenant = Tenant(name=name)
        db_session.add(tenant)
        db_session.flush()
        issued = api_keys.issue(
            db_session, tenant_id=tenant.id, name=f"{name}-key", is_admin=is_admin
        )
        return tenant, {"Authorization": f"Bearer {issued.secret}"}

    return _make


@pytest.fixture
def admin_headers(make_tenant) -> dict[str, str]:
    _, headers = make_tenant("admin-tenant", is_admin=True)
    return headers
