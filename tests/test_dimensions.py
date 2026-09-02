"""Step 17: dimensions — what a question groups or filters by."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.models import (
    Dimension,
    DimensionDataType,
    Entity,
    SemanticModel,
    SemanticModelVersion,
    Tenant,
    TimeGranularity,
    VersionStatus,
    Workspace,
)


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _seed_entity(db: Session, tenant_name: str, entity_name: str = "customer") -> Entity:
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

    entity = Entity(
        tenant_id=tenant.id,
        semantic_model_version_id=version.id,
        name=entity_name,
        source_table=f"{entity_name}s",
    )
    db.add(entity)
    db.flush()
    return entity


def _dimension(db: Session, entity: Entity, **kwargs) -> Dimension:
    defaults = {
        "tenant_id": entity.tenant_id,
        "entity_id": entity.id,
        "name": "state",
        "expression": "state",
    }
    dimension = Dimension(**{**defaults, **kwargs})
    db.add(dimension)
    db.flush()
    return dimension


def test_defaults(db_session: Session) -> None:
    entity = _seed_entity(db_session, "acme")
    dimension = _dimension(db_session, entity)
    db_session.refresh(dimension)

    assert dimension.data_type == DimensionDataType.STRING
    assert dimension.granularity is None
    assert dimension.is_time_dimension is False


def test_data_type_is_stored_as_its_value(db_session: Session) -> None:
    entity = _seed_entity(db_session, "acme")
    _dimension(db_session, entity, name="signed_up", expression="signed_up_at",
               data_type=DimensionDataType.TIMESTAMP)
    assert db_session.execute(text("SELECT data_type FROM dimensions")).scalar_one() == (
        "timestamp"
    )


def test_invalid_data_type_is_rejected(db_session: Session) -> None:
    entity = _seed_entity(db_session, "acme")
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO dimensions (id, tenant_id, entity_id, name, expression, "
                "data_type, created_at, updated_at) "
                "VALUES (:i, :t, :e, 'x', 'x', 'nonsense', now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(entity.tenant_id), "e": str(entity.id)},
        )


def test_time_dimension_is_derived_not_stored(db_session: Session) -> None:
    """A stored flag could disagree with data_type; a property cannot."""
    entity = _seed_entity(db_session, "acme")
    date_dim = _dimension(db_session, entity, name="order_date", expression="order_date",
                          data_type=DimensionDataType.DATE)
    text_dim = _dimension(db_session, entity, name="state", expression="state")

    assert date_dim.is_time_dimension is True
    assert text_dim.is_time_dimension is False


def test_granularity_is_allowed_on_a_time_dimension(db_session: Session) -> None:
    entity = _seed_entity(db_session, "acme")
    dimension = _dimension(db_session, entity, name="order_date", expression="order_date",
                           data_type=DimensionDataType.DATE,
                           granularity=TimeGranularity.MONTH)
    assert dimension.granularity is TimeGranularity.MONTH


@pytest.mark.parametrize(
    "data_type", [DimensionDataType.STRING, DimensionDataType.NUMBER, DimensionDataType.BOOLEAN]
)
def test_granularity_on_a_non_time_dimension_is_rejected(
    db_session: Session, data_type: DimensionDataType
) -> None:
    """Otherwise a query could ask to bucket a string by month."""
    entity = _seed_entity(db_session, "acme")
    with pytest.raises(IntegrityError):
        _dimension(db_session, entity, data_type=data_type,
                   granularity=TimeGranularity.MONTH)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expression", "state; DROP TABLE tenants--"),
        ("expression", "state, password"),
        ("expression", "UPPER(state)"),
        ("expression", ""),
        ("name", "customer-state"),
        ("name", "1_leading_digit"),
    ],
)
def test_non_identifier_values_are_rejected(
    db_session: Session, field: str, value: str
) -> None:
    entity = _seed_entity(db_session, "acme")
    with pytest.raises(IntegrityError):
        _dimension(db_session, entity, **{field: value})


def test_names_are_unique_within_an_entity(db_session: Session) -> None:
    entity = _seed_entity(db_session, "acme")
    _dimension(db_session, entity, name="state")
    with pytest.raises(IntegrityError):
        _dimension(db_session, entity, name="state", expression="region")


def test_same_name_allowed_on_a_different_entity(db_session: Session) -> None:
    entity = _seed_entity(db_session, "acme")
    other = Entity(
        tenant_id=entity.tenant_id,
        semantic_model_version_id=entity.semantic_model_version_id,
        name="supplier",
        source_table="suppliers",
    )
    db_session.add(other)
    db_session.flush()

    _dimension(db_session, entity, name="state")
    _dimension(db_session, other, name="state")
    assert db_session.execute(text("SELECT count(*) FROM dimensions")).scalar_one() == 2


def test_dimension_cannot_claim_a_different_tenant(db_session: Session) -> None:
    entity = _seed_entity(db_session, "acme")
    other_tenant = Tenant(name="globex")
    db_session.add(other_tenant)
    db_session.flush()
    _scope(db_session, entity.tenant_id)

    with pytest.raises((IntegrityError, ProgrammingError)):
        db_session.execute(
            text(
                "INSERT INTO dimensions (id, tenant_id, entity_id, name, expression, "
                "data_type, created_at, updated_at) "
                "VALUES (:i, :t, :e, 'x', 'x', 'string', now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(other_tenant.id), "e": str(entity.id)},
        )


def test_rls_isolates_dimensions(db_session: Session) -> None:
    acme_entity = _seed_entity(db_session, "acme")
    globex_entity = _seed_entity(db_session, "globex")
    _dimension(db_session, globex_entity, name="region", expression="region")

    _scope(db_session, acme_entity.tenant_id)
    _dimension(db_session, acme_entity, name="state", expression="state")

    names = db_session.execute(text("SELECT name FROM dimensions")).scalars().all()
    assert names == ["state"]


def test_deleting_an_entity_cascades_to_dimensions(db_session: Session) -> None:
    entity = _seed_entity(db_session, "acme")
    _dimension(db_session, entity, name="state")
    _dimension(db_session, entity, name="industry", expression="industry")

    db_session.delete(entity)
    db_session.flush()
    assert db_session.execute(text("SELECT count(*) FROM dimensions")).scalar_one() == 0
