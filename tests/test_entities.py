"""Step 16: entities — the binding between semantic names and physical tables."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.models import (
    Entity,
    EntityKind,
    SemanticModel,
    SemanticModelVersion,
    Tenant,
    VersionStatus,
    Workspace,
)


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _seed_version(db: Session, tenant_name: str) -> tuple[Tenant, SemanticModelVersion]:
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

    version = SemanticModelVersion(
        tenant_id=tenant.id,
        semantic_model_id=model.id,
        version=1,
        status=VersionStatus.DRAFT,
    )
    db.add(version)
    db.flush()
    return tenant, version


def _entity(db: Session, version: SemanticModelVersion, **kwargs) -> Entity:
    defaults = {
        "tenant_id": version.tenant_id,
        "semantic_model_version_id": version.id,
        "name": "customer",
        "source_table": "customers",
    }
    entity = Entity(**{**defaults, **kwargs})
    db.add(entity)
    db.flush()
    return entity


def test_entity_defaults(db_session: Session) -> None:
    _, version = _seed_version(db_session, "acme")
    entity = _entity(db_session, version)
    db_session.refresh(entity)

    assert entity.source_schema == "public"
    assert entity.primary_key == "id"
    assert entity.kind == EntityKind.DIMENSION
    assert entity.qualified_table == "public.customers"


def test_kind_is_stored_as_its_value(db_session: Session) -> None:
    _, version = _seed_version(db_session, "acme")
    _entity(db_session, version, kind=EntityKind.FACT)
    assert db_session.execute(text("SELECT kind FROM entities")).scalar_one() == "fact"


def test_invalid_kind_is_rejected(db_session: Session) -> None:
    _, version = _seed_version(db_session, "acme")
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO entities (id, tenant_id, semantic_model_version_id, name, "
                "source_schema, source_table, primary_key, kind, created_at, updated_at) "
                "VALUES (:i, :t, :v, 'x', 'public', 'y', 'id', 'nonsense', now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(version.tenant_id), "v": str(version.id)},
        )


@pytest.mark.parametrize(
    "column,value",
    [
        ("source_table", "orders; DROP TABLE tenants--"),
        ("source_table", "orders WHERE 1=1"),
        ("source_schema", "public.evil"),
        ("primary_key", "id, password"),
        ("name", "customer-name"),
        ("source_table", "1_starts_with_digit"),
        ("source_table", ""),
    ],
)
def test_non_identifier_values_are_rejected(
    db_session: Session, column: str, value: str
) -> None:
    """These strings reach the SQL compiler as identifiers. The database is the
    last line of defence if application validation is ever bypassed."""
    _, version = _seed_version(db_session, "acme")
    with pytest.raises(IntegrityError):
        _entity(db_session, version, **{column: value})


def test_ordinary_identifiers_are_accepted(db_session: Session) -> None:
    _, version = _seed_version(db_session, "acme")
    entity = _entity(
        db_session,
        version,
        name="order_item",
        source_schema="analytics",
        source_table="Order_Items_2024",
        primary_key="order_item_id",
    )
    assert entity.qualified_table == "analytics.Order_Items_2024"


def test_entity_names_are_unique_within_a_version(db_session: Session) -> None:
    _, version = _seed_version(db_session, "acme")
    _entity(db_session, version, name="customer")
    with pytest.raises(IntegrityError):
        _entity(db_session, version, name="customer", source_table="other")


def test_same_entity_name_allowed_in_a_different_version(db_session: Session) -> None:
    tenant, version = _seed_version(db_session, "acme")
    _entity(db_session, version, name="customer")

    version_two = SemanticModelVersion(
        tenant_id=tenant.id,
        semantic_model_id=version.semantic_model_id,
        version=2,
        status=VersionStatus.DRAFT,
    )
    db_session.add(version_two)
    db_session.flush()
    _entity(db_session, version_two, name="customer")

    assert db_session.execute(text("SELECT count(*) FROM entities")).scalar_one() == 2


def test_entity_cannot_claim_a_different_tenant(db_session: Session) -> None:
    _, version = _seed_version(db_session, "acme")
    other = Tenant(name="globex")
    db_session.add(other)
    db_session.flush()
    _scope(db_session, version.tenant_id)

    with pytest.raises((IntegrityError, ProgrammingError)):
        db_session.execute(
            text(
                "INSERT INTO entities (id, tenant_id, semantic_model_version_id, name, "
                "source_schema, source_table, primary_key, kind, created_at, updated_at) "
                "VALUES (:i, :t, :v, 'x', 'public', 'y', 'id', 'fact', now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(other.id), "v": str(version.id)},
        )


def test_rls_isolates_entities(db_session: Session) -> None:
    acme, acme_version = _seed_version(db_session, "acme")
    _, globex_version = _seed_version(db_session, "globex")
    _entity(db_session, globex_version, name="supplier", source_table="suppliers")

    _scope(db_session, acme.id)
    _entity(db_session, acme_version, name="customer")

    names = db_session.execute(text("SELECT name FROM entities")).scalars().all()
    assert names == ["customer"]


def test_deleting_a_version_cascades_to_entities(db_session: Session) -> None:
    _, version = _seed_version(db_session, "acme")
    _entity(db_session, version, name="customer")
    _entity(db_session, version, name="order", source_table="orders")

    db_session.delete(version)
    db_session.flush()
    assert db_session.execute(text("SELECT count(*) FROM entities")).scalar_one() == 0
