"""Step 16b: data sources, and a live connection to a real warehouse.

The integration tests point a data source at the project's own Postgres
container. That is not the intended production shape, but it is a genuine
second database from the application's point of view, and it proves the
read-only enforcement works against a server we do not configure per-query.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_secret
from app.models import (
    DataSource,
    DataSourceDialect,
    SemanticModel,
    SemanticModelVersion,
    Tenant,
    VersionStatus,
    Workspace,
)
from app.services import warehouse


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _seed(db: Session, tenant_name: str) -> tuple[Tenant, Workspace, SemanticModel]:
    tenant = Tenant(name=tenant_name)
    db.add(tenant)
    db.flush()
    _scope(db, tenant.id)

    workspace = Workspace(tenant_id=tenant.id, name="analytics")
    db.add(workspace)
    db.flush()

    model = SemanticModel(tenant_id=tenant.id, workspace_id=workspace.id, name="Sales")
    db.add(model)
    db.flush()
    return tenant, workspace, model


def _data_source(db: Session, workspace: Workspace, **kwargs) -> DataSource:
    ciphertext, key_version = encrypt_secret(kwargs.pop("password", "postgres"))
    defaults = {
        "tenant_id": workspace.tenant_id,
        "workspace_id": workspace.id,
        "name": "warehouse",
        "host": "db",
        "port": 5432,
        "database": "semantic_model",
        "username": "postgres",
        "password_ciphertext": ciphertext,
        "key_version": key_version,
    }
    source = DataSource(**{**defaults, **kwargs})
    db.add(source)
    db.flush()
    return source


def test_defaults(db_session: Session) -> None:
    _, workspace, _ = _seed(db_session, "acme")
    source = _data_source(db_session, workspace)
    db_session.refresh(source)
    assert source.dialect == DataSourceDialect.POSTGRESQL
    assert source.default_schema == "public"
    assert source.port == 5432


def test_password_is_never_stored_in_plaintext(db_session: Session) -> None:
    _, workspace, _ = _seed(db_session, "acme")
    _data_source(db_session, workspace, password="sup3rs3cret")

    stored = db_session.execute(
        text("SELECT password_ciphertext FROM data_sources")
    ).scalar_one()
    assert b"sup3rs3cret" not in bytes(stored)


def test_repr_does_not_leak_credentials(db_session: Session) -> None:
    _, workspace, _ = _seed(db_session, "acme")
    source = _data_source(db_session, workspace, password="sup3rs3cret", username="admin")
    assert "sup3rs3cret" not in repr(source)


@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_invalid_ports_are_rejected(db_session: Session, port: int) -> None:
    _, workspace, _ = _seed(db_session, "acme")
    with pytest.raises(IntegrityError):
        _data_source(db_session, workspace, port=port)


def test_non_identifier_default_schema_is_rejected(db_session: Session) -> None:
    _, workspace, _ = _seed(db_session, "acme")
    with pytest.raises(IntegrityError):
        _data_source(db_session, workspace, default_schema="public; DROP TABLE x--")


def test_rls_isolates_data_sources(db_session: Session) -> None:
    acme, acme_ws, _ = _seed(db_session, "acme")
    _, globex_ws, _ = _seed(db_session, "globex")
    _data_source(db_session, globex_ws, name="globex-warehouse")

    _scope(db_session, acme.id)
    _data_source(db_session, acme_ws, name="acme-warehouse")

    names = db_session.execute(text("SELECT name FROM data_sources")).scalars().all()
    assert names == ["acme-warehouse"]


# ---------- the published-version invariant ----------


def _version(db: Session, model: SemanticModel, **kwargs) -> SemanticModelVersion:
    defaults = {
        "tenant_id": model.tenant_id,
        "semantic_model_id": model.id,
        "version": 1,
        "status": VersionStatus.DRAFT,
    }
    version = SemanticModelVersion(**{**defaults, **kwargs})
    db.add(version)
    db.flush()
    return version


def test_a_draft_may_have_no_data_source(db_session: Session) -> None:
    _, _, model = _seed(db_session, "acme")
    version = _version(db_session, model)
    assert version.data_source_id is None


def test_a_published_version_must_name_a_data_source(db_session: Session) -> None:
    _, _, model = _seed(db_session, "acme")
    with pytest.raises(IntegrityError):
        _version(db_session, model, status=VersionStatus.PUBLISHED)


def test_a_published_version_with_a_data_source_is_accepted(db_session: Session) -> None:
    _, workspace, model = _seed(db_session, "acme")
    source = _data_source(db_session, workspace)
    version = _version(db_session, model, status=VersionStatus.PUBLISHED,
                       data_source_id=source.id)
    assert version.data_source_id == source.id


def test_a_version_cannot_borrow_another_tenants_data_source(db_session: Session) -> None:
    _, _, acme_model = _seed(db_session, "acme")
    _, globex_ws, _ = _seed(db_session, "globex")
    globex_source = _data_source(db_session, globex_ws)

    _scope(db_session, acme_model.tenant_id)
    with pytest.raises((IntegrityError, ProgrammingError)):
        _version(db_session, acme_model, data_source_id=globex_source.id)


# ---------- live warehouse connection ----------


@pytest.fixture
def live_source(db_session: Session) -> DataSource:
    _, workspace, _ = _seed(db_session, "acme")
    return _data_source(db_session, workspace)


def test_verify_connects(live_source: DataSource) -> None:
    warehouse.verify(live_source)


def test_introspection_lists_tables(live_source: DataSource) -> None:
    tables = warehouse.list_tables(live_source)
    assert "tenants" in tables and "entities" in tables


def test_introspection_lists_columns(live_source: DataSource) -> None:
    columns = warehouse.list_columns(live_source, "entities")
    assert {"id", "tenant_id", "source_table"} <= set(columns)


def test_missing_table_reports_no_columns(live_source: DataSource) -> None:
    assert warehouse.list_columns(live_source, "no_such_table") == []
    assert warehouse.table_exists(live_source, "no_such_table") is False


def test_the_connection_refuses_writes_even_as_superuser(live_source: DataSource) -> None:
    """default_transaction_read_only is enforced by the warehouse's own server,
    so a compiler bug cannot write even with superuser credentials."""
    engine = warehouse.get_engine(live_source)
    with engine.connect() as connection, pytest.raises(DBAPIError) as exc:
        connection.execute(text("CREATE TABLE should_not_exist (id int)"))
    assert "read-only" in str(exc.value).lower()


def test_engines_are_cached_per_data_source(live_source: DataSource) -> None:
    assert warehouse.get_engine(live_source) is warehouse.get_engine(live_source)


def test_url_escapes_hostile_passwords(db_session: Session) -> None:
    _, workspace, _ = _seed(db_session, "acme")
    source = _data_source(db_session, workspace, password="p@ss:w/rd?#")
    assert warehouse.build_url(source).password == "p@ss:w/rd?#"
