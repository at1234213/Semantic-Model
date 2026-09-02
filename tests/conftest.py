from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
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
