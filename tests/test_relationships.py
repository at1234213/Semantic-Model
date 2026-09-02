"""Step 19: the join graph."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.models import (
    Cardinality,
    Entity,
    JoinType,
    Relationship,
    RelationshipJoinKey,
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


def _seed(db: Session, tenant_name: str = "acme") -> SemanticModelVersion:
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
        tenant_id=tenant.id, semantic_model_id=model.id, version=1,
        status=VersionStatus.DRAFT,
    )
    db.add(version)
    db.flush()
    return version


def _entity(db: Session, version: SemanticModelVersion, name: str) -> Entity:
    entity = Entity(
        tenant_id=version.tenant_id,
        semantic_model_version_id=version.id,
        name=name,
        source_table=f"{name}s",
    )
    db.add(entity)
    db.flush()
    return entity


def _edge(db: Session, version: SemanticModelVersion, frm: Entity, to: Entity,
          name: str = "orders_to_customers", **kwargs) -> Relationship:
    edge = Relationship(
        tenant_id=version.tenant_id,
        semantic_model_version_id=version.id,
        from_entity_id=frm.id,
        to_entity_id=to.id,
        name=name,
        **kwargs,
    )
    db.add(edge)
    db.flush()
    return edge


def _key(db: Session, edge: Relationship, frm: str, to: str, ordinal: int = 0):
    key = RelationshipJoinKey(
        tenant_id=edge.tenant_id,
        relationship_id=edge.id,
        from_column=frm,
        to_column=to,
        ordinal=ordinal,
    )
    db.add(key)
    db.flush()
    return key


def test_defaults(db_session: Session) -> None:
    version = _seed(db_session)
    edge = _edge(db_session, version, _entity(db_session, version, "order"),
                 _entity(db_session, version, "customer"))
    db_session.refresh(edge)
    assert edge.join_type == JoinType.INNER
    assert edge.cardinality == Cardinality.MANY_TO_ONE


def test_enums_store_their_values(db_session: Session) -> None:
    version = _seed(db_session)
    _edge(db_session, version, _entity(db_session, version, "order"),
          _entity(db_session, version, "customer"),
          join_type=JoinType.LEFT, cardinality=Cardinality.ONE_TO_MANY)
    row = db_session.execute(
        text("SELECT join_type, cardinality FROM relationships")
    ).one()
    assert row.join_type == "left"
    assert row.cardinality == "one_to_many"


def test_composite_join_keys_stay_ordered(db_session: Session) -> None:
    version = _seed(db_session)
    edge = _edge(db_session, version, _entity(db_session, version, "line"),
                 _entity(db_session, version, "shipment"))
    _key(db_session, edge, "tenant_code", "tenant_code", ordinal=1)
    _key(db_session, edge, "order_id", "order_id", ordinal=0)

    db_session.refresh(edge)
    assert [(k.from_column, k.to_column) for k in edge.join_keys] == [
        ("order_id", "order_id"),
        ("tenant_code", "tenant_code"),
    ]


def test_duplicate_ordinal_is_rejected(db_session: Session) -> None:
    version = _seed(db_session)
    edge = _edge(db_session, version, _entity(db_session, version, "order"),
                 _entity(db_session, version, "customer"))
    _key(db_session, edge, "customer_id", "id", ordinal=0)
    with pytest.raises(IntegrityError):
        _key(db_session, edge, "other_id", "id", ordinal=0)


@pytest.mark.parametrize("column", ["from_column", "to_column"])
@pytest.mark.parametrize("value", ["id; DROP TABLE tenants--", "c.id", "id, secret", ""])
def test_non_identifier_join_columns_are_rejected(
    db_session: Session, column: str, value: str
) -> None:
    version = _seed(db_session)
    edge = _edge(db_session, version, _entity(db_session, version, "order"),
                 _entity(db_session, version, "customer"))
    columns = {"from_column": "customer_id", "to_column": "id"}
    columns[column] = value
    with pytest.raises(IntegrityError):
        _key(db_session, edge, columns["from_column"], columns["to_column"])


def test_self_join_is_rejected(db_session: Session) -> None:
    version = _seed(db_session)
    order = _entity(db_session, version, "order")
    with pytest.raises(IntegrityError):
        _edge(db_session, version, order, order)


def test_one_edge_per_ordered_pair(db_session: Session) -> None:
    """Two roles over one table are modelled as two entities, not two edges."""
    version = _seed(db_session)
    order = _entity(db_session, version, "order")
    customer = _entity(db_session, version, "customer")
    _edge(db_session, version, order, customer, name="a")
    with pytest.raises(IntegrityError):
        _edge(db_session, version, order, customer, name="b")


def test_the_reverse_direction_is_a_separate_edge(db_session: Session) -> None:
    version = _seed(db_session)
    order = _entity(db_session, version, "order")
    customer = _entity(db_session, version, "customer")
    _edge(db_session, version, order, customer, name="order_to_customer")
    _edge(db_session, version, customer, order, name="customer_to_order",
          cardinality=Cardinality.ONE_TO_MANY)
    assert db_session.execute(text("SELECT count(*) FROM relationships")).scalar_one() == 2


def test_edge_cannot_span_two_versions(db_session: Session) -> None:
    """The three-column keys pin both endpoints to one version."""
    version = _seed(db_session)
    order = _entity(db_session, version, "order")

    other_version = SemanticModelVersion(
        tenant_id=version.tenant_id, semantic_model_id=version.semantic_model_id,
        version=2, status=VersionStatus.DRAFT,
    )
    db_session.add(other_version)
    db_session.flush()
    foreign_customer = _entity(db_session, other_version, "customer")

    with pytest.raises(IntegrityError):
        _edge(db_session, version, order, foreign_customer)


def test_edge_cannot_claim_a_different_tenant(db_session: Session) -> None:
    version = _seed(db_session)
    order = _entity(db_session, version, "order")
    customer = _entity(db_session, version, "customer")
    other = Tenant(name="globex")
    db_session.add(other)
    db_session.flush()
    _scope(db_session, version.tenant_id)

    with pytest.raises((IntegrityError, ProgrammingError)):
        db_session.execute(
            text(
                "INSERT INTO relationships (id, tenant_id, semantic_model_version_id, "
                "from_entity_id, to_entity_id, name, join_type, cardinality, "
                "created_at, updated_at) "
                "VALUES (:i, :t, :v, :f, :o, 'x', 'inner', 'many_to_one', now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(other.id), "v": str(version.id),
             "f": str(order.id), "o": str(customer.id)},
        )


def test_rls_isolates_relationships(db_session: Session) -> None:
    acme = _seed(db_session, "acme")
    globex = _seed(db_session, "globex")
    _edge(db_session, globex, _entity(db_session, globex, "order"),
          _entity(db_session, globex, "customer"), name="globex_edge")

    _scope(db_session, acme.tenant_id)
    _edge(db_session, acme, _entity(db_session, acme, "order"),
          _entity(db_session, acme, "customer"), name="acme_edge")

    names = db_session.execute(text("SELECT name FROM relationships")).scalars().all()
    assert names == ["acme_edge"]


def test_deleting_an_entity_cascades_to_its_edges_and_keys(db_session: Session) -> None:
    version = _seed(db_session)
    order = _entity(db_session, version, "order")
    customer = _entity(db_session, version, "customer")
    edge = _edge(db_session, version, order, customer)
    _key(db_session, edge, "customer_id", "id")

    db_session.delete(customer)
    db_session.flush()
    assert db_session.execute(text("SELECT count(*) FROM relationships")).scalar_one() == 0
    assert db_session.execute(
        text("SELECT count(*) FROM relationship_join_keys")
    ).scalar_one() == 0


def test_version_exposes_its_edges(db_session: Session) -> None:
    version = _seed(db_session)
    _edge(db_session, version, _entity(db_session, version, "order"),
          _entity(db_session, version, "customer"))
    db_session.refresh(version)
    assert len(version.relationships) == 1
