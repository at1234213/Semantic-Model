"""Step 20: business rules, glossary, synonyms — the last of the schema."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    BusinessRule,
    Dimension,
    Entity,
    FilterOperator,
    GlossaryTerm,
    Metric,
    SemanticModel,
    SemanticModelVersion,
    Synonym,
    Tenant,
    VersionStatus,
    Workspace,
)


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _seed(db: Session, tenant_name: str = "acme") -> tuple[SemanticModelVersion, Entity]:
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
    entity = Entity(
        tenant_id=tenant.id, semantic_model_version_id=version.id,
        name="customer", source_table="customers",
    )
    db.add(entity)
    db.flush()
    return version, entity


def _rule(db: Session, version: SemanticModelVersion, entity: Entity, **kwargs) -> BusinessRule:
    defaults = {
        "tenant_id": version.tenant_id,
        "semantic_model_version_id": version.id,
        "entity_id": entity.id,
        "name": "exclude_internal",
        "column_name": "is_internal",
        "operator": FilterOperator.EQ,
        "value": False,
    }
    rule = BusinessRule(**{**defaults, **kwargs})
    db.add(rule)
    db.flush()
    return rule


# ---------- business rules ----------


def test_rule_defaults_to_active(db_session: Session) -> None:
    version, entity = _seed(db_session)
    rule = _rule(db_session, version, entity)
    db_session.refresh(rule)
    assert rule.is_active is True


@pytest.mark.parametrize(
    "operator,value",
    [
        (FilterOperator.EQ, False),
        (FilterOperator.NE, "test"),
        (FilterOperator.GT, 100),
        (FilterOperator.IN, ["a", "b"]),
        (FilterOperator.NOT_IN, [1, 2, 3]),
    ],
)
def test_values_round_trip_through_jsonb(
    db_session: Session, operator: FilterOperator, value: object
) -> None:
    """The value is data, never SQL text — it becomes a bind parameter."""
    version, entity = _seed(db_session)
    rule = _rule(db_session, version, entity, operator=operator, value=value)
    db_session.refresh(rule)
    assert rule.value == value


@pytest.mark.parametrize("operator", [FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL])
def test_nullary_operators_take_no_value(
    db_session: Session, operator: FilterOperator
) -> None:
    version, entity = _seed(db_session)
    _rule(db_session, version, entity, operator=operator, value=None)

    with pytest.raises(IntegrityError):
        _rule(db_session, version, entity, name="bad", operator=operator, value="x")


def test_comparison_operators_require_a_value(db_session: Session) -> None:
    version, entity = _seed(db_session)
    with pytest.raises(IntegrityError):
        _rule(db_session, version, entity, operator=FilterOperator.EQ, value=None)


def test_invalid_operator_is_rejected(db_session: Session) -> None:
    version, entity = _seed(db_session)
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO business_rules (id, tenant_id, semantic_model_version_id, "
                "entity_id, name, column_name, operator, value, is_active, "
                "created_at, updated_at) VALUES "
                "(:i, :t, :v, :e, 'x', 'y', 'nonsense', 'null'::jsonb, true, now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(version.tenant_id), "v": str(version.id),
             "e": str(entity.id)},
        )


@pytest.mark.parametrize("column", ["is_internal; DROP TABLE x--", "a.b", "a, b", ""])
def test_non_identifier_column_is_rejected(db_session: Session, column: str) -> None:
    version, entity = _seed(db_session)
    with pytest.raises(IntegrityError):
        _rule(db_session, version, entity, column_name=column)


def test_rule_cannot_target_an_entity_from_another_version(db_session: Session) -> None:
    version, entity = _seed(db_session)
    other = SemanticModelVersion(
        tenant_id=version.tenant_id, semantic_model_id=version.semantic_model_id,
        version=2, status=VersionStatus.DRAFT,
    )
    db_session.add(other)
    db_session.flush()
    foreign_entity = Entity(
        tenant_id=version.tenant_id, semantic_model_version_id=other.id,
        name="order", source_table="orders",
    )
    db_session.add(foreign_entity)
    db_session.flush()

    with pytest.raises(IntegrityError):
        _rule(db_session, version, foreign_entity)


# ---------- glossary ----------


def test_glossary_accepts_multi_word_terms(db_session: Session) -> None:
    version, _ = _seed(db_session)
    term = GlossaryTerm(
        tenant_id=version.tenant_id,
        semantic_model_version_id=version.id,
        term="active customer",
        definition="A customer with an order in the last 90 days.",
    )
    db_session.add(term)
    db_session.flush()
    assert term.term == "active customer"


def test_glossary_rejects_blank_term_or_definition(db_session: Session) -> None:
    version, _ = _seed(db_session)
    for term, definition in [("   ", "x"), ("x", "  ")]:
        db_session.add(
            GlossaryTerm(
                tenant_id=version.tenant_id,
                semantic_model_version_id=version.id,
                term=term,
                definition=definition,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()
        _scope(db_session, version.tenant_id)


def test_glossary_terms_are_unique_per_version(db_session: Session) -> None:
    version, _ = _seed(db_session)
    for _ in range(2):
        db_session.add(
            GlossaryTerm(
                tenant_id=version.tenant_id,
                semantic_model_version_id=version.id,
                term="churn",
                definition="d",
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------- synonyms ----------


def _metric(db: Session, version: SemanticModelVersion, name: str = "revenue") -> Metric:
    metric = Metric(
        tenant_id=version.tenant_id, semantic_model_version_id=version.id,
        name=name, expression="${gross_revenue}",
    )
    db.add(metric)
    db.flush()
    return metric


def test_synonym_points_at_a_metric(db_session: Session) -> None:
    version, _ = _seed(db_session)
    metric = _metric(db_session, version)
    synonym = Synonym(
        tenant_id=version.tenant_id, semantic_model_version_id=version.id,
        term="top line", metric_id=metric.id,
    )
    db_session.add(synonym)
    db_session.flush()
    assert synonym.target_id == metric.id


def test_synonym_must_have_exactly_one_target(db_session: Session) -> None:
    version, entity = _seed(db_session)
    metric = _metric(db_session, version)

    # none
    db_session.add(
        Synonym(tenant_id=version.tenant_id, semantic_model_version_id=version.id, term="a")
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    _scope(db_session, version.tenant_id)

    # two
    db_session.add(
        Synonym(
            tenant_id=version.tenant_id, semantic_model_version_id=version.id,
            term="b", entity_id=entity.id, metric_id=metric.id,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_synonym_can_point_at_a_dimension(db_session: Session) -> None:
    version, entity = _seed(db_session)
    dimension = Dimension(
        tenant_id=version.tenant_id, entity_id=entity.id, name="state", expression="state"
    )
    db_session.add(dimension)
    db_session.flush()

    synonym = Synonym(
        tenant_id=version.tenant_id, semantic_model_version_id=version.id,
        term="province", dimension_id=dimension.id,
    )
    db_session.add(synonym)
    db_session.flush()
    assert synonym.target_id == dimension.id


def test_synonym_terms_are_unique_per_version(db_session: Session) -> None:
    version, _ = _seed(db_session)
    metric = _metric(db_session, version)
    other = _metric(db_session, version, "margin")
    for target in (metric, other):
        db_session.add(
            Synonym(
                tenant_id=version.tenant_id, semantic_model_version_id=version.id,
                term="sales", metric_id=target.id,
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------- isolation ----------


def test_rls_isolates_all_three(db_session: Session) -> None:
    acme_version, acme_entity = _seed(db_session, "acme")
    globex_version, globex_entity = _seed(db_session, "globex")
    _rule(db_session, globex_version, globex_entity, name="globex_rule")
    db_session.add(
        GlossaryTerm(tenant_id=globex_version.tenant_id,
                     semantic_model_version_id=globex_version.id,
                     term="globex", definition="d")
    )
    db_session.flush()

    _scope(db_session, acme_version.tenant_id)
    _rule(db_session, acme_version, acme_entity, name="acme_rule")

    assert db_session.execute(
        text("SELECT name FROM business_rules")
    ).scalars().all() == ["acme_rule"]
    assert db_session.execute(text("SELECT count(*) FROM glossary_terms")).scalar_one() == 0
