"""Step 27: join-path resolution and deterministic SQL compilation."""

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    Aggregation,
    BusinessRule,
    Cardinality,
    Dimension,
    DimensionDataType,
    Entity,
    FilterOperator,
    Measure,
    Metric,
    Relationship,
    RelationshipJoinKey,
    SemanticModel,
    SemanticModelVersion,
    Tenant,
    VersionStatus,
    Workspace,
)
from app.services.compiler import CompilationError, compile_query
from app.services.intent import (
    FilterOperatorPhrase,
    FilterPhrase,
    Intent,
    IntentType,
    TimeRangePhrase,
)
from app.services.joins import JoinPathError, resolve_join_path
from app.services.metrics import set_expression
from app.services.query_ir import build
from app.services.resolution import resolve
from app.services.retrieval import rebuild_search_index

TODAY = date(2026, 9, 2)


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


@pytest.fixture
def model(db_session: Session):
    """orders → customers, one measure, two metrics, one business rule."""
    db = db_session
    tenant = Tenant(name="acme")
    db.add(tenant)
    db.flush()
    _scope(db, tenant.id)

    workspace = Workspace(tenant_id=tenant.id, name="analytics")
    db.add(workspace)
    db.flush()
    sm = SemanticModel(tenant_id=tenant.id, workspace_id=workspace.id, name="Sales")
    db.add(sm)
    db.flush()
    version = SemanticModelVersion(
        tenant_id=tenant.id, semantic_model_id=sm.id, version=1,
        status=VersionStatus.DRAFT,
    )
    db.add(version)
    db.flush()

    order = Entity(tenant_id=tenant.id, semantic_model_version_id=version.id,
                   name="order", source_table="orders")
    customer = Entity(tenant_id=tenant.id, semantic_model_version_id=version.id,
                      name="customer", source_table="customers")
    db.add_all([order, customer])
    db.flush()

    edge = Relationship(
        tenant_id=tenant.id, semantic_model_version_id=version.id,
        from_entity_id=order.id, to_entity_id=customer.id,
        name="order_to_customer", cardinality=Cardinality.MANY_TO_ONE,
    )
    db.add(edge)
    db.flush()
    db.add(RelationshipJoinKey(tenant_id=tenant.id, relationship_id=edge.id,
                               from_column="customer_id", to_column="id"))

    db.add_all([
        Dimension(tenant_id=tenant.id, entity_id=customer.id, name="country",
                  expression="country", description="Billing country"),
        Dimension(tenant_id=tenant.id, entity_id=order.id, name="order_date",
                  expression="order_date", data_type=DimensionDataType.DATE,
                  description="When the order was placed"),
    ])
    db.add_all([
        Measure(tenant_id=tenant.id, entity_id=order.id,
                semantic_model_version_id=version.id, name="gross_revenue",
                aggregation=Aggregation.SUM, expression="amount"),
        Measure(tenant_id=tenant.id, entity_id=order.id,
                semantic_model_version_id=version.id, name="order_count",
                aggregation=Aggregation.COUNT_DISTINCT, expression="id"),
    ])
    db.flush()

    revenue = Metric(tenant_id=tenant.id, semantic_model_version_id=version.id,
                     name="revenue", expression="${gross_revenue}",
                     description="Total money billed")
    aov = Metric(tenant_id=tenant.id, semantic_model_version_id=version.id,
                 name="average_order_value", expression="${gross_revenue}",
                 description="Mean value of an order")
    db.add_all([revenue, aov])
    db.flush()
    set_expression(db, revenue, "${gross_revenue}")
    set_expression(db, aov, "${gross_revenue} / ${order_count}")

    db.add(BusinessRule(
        tenant_id=tenant.id, semantic_model_version_id=version.id,
        entity_id=customer.id, name="exclude_internal", column_name="is_internal",
        operator=FilterOperator.EQ, value=False,
    ))
    db.flush()
    rebuild_search_index(db, version.id)
    return version, order, customer


def _compile(db: Session, version, intent: Intent):
    result = build(db, resolve(db, intent, version_id=version.id),
                   version_id=version.id, today=TODAY)
    assert result.ok, result.problems
    return compile_query(db, result.query)


# ---------- join resolution ----------


def test_two_entities_are_connected(db_session: Session, model) -> None:
    version, order, customer = model
    plan = resolve_join_path(db_session, version_id=version.id,
                             required_entity_ids=[order.id, customer.id],
                             root_entity_id=order.id)
    assert [e.name for e in plan.entities] == ["order", "customer"]


def test_a_single_entity_needs_no_joins(db_session: Session, model) -> None:
    version, order, _ = model
    plan = resolve_join_path(db_session, version_id=version.id,
                             required_entity_ids=[order.id], root_entity_id=order.id)
    assert plan.steps == []


def test_an_unconnected_entity_is_reported_not_invented(db_session: Session, model) -> None:
    """Inventing a join is how a query returns a number from a cartesian product."""
    version, order, _ = model
    orphan = Entity(tenant_id=version.tenant_id, semantic_model_version_id=version.id,
                    name="supplier", source_table="suppliers")
    db_session.add(orphan)
    db_session.flush()

    with pytest.raises(JoinPathError) as exc:
        resolve_join_path(db_session, version_id=version.id,
                          required_entity_ids=[order.id, orphan.id],
                          root_entity_id=order.id)
    assert "supplier" in str(exc.value)


# ---------- compilation ----------


def test_a_simple_aggregate_compiles(db_session: Session, model) -> None:
    version, _, _ = model
    compiled = _compile(db_session, version,
                        Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]))

    assert compiled.sql.startswith("SELECT")
    assert "SUM(e0.amount)" in compiled.sql
    assert "FROM   public.orders AS e0" in compiled.sql


def test_a_breakdown_joins_and_groups(db_session: Session, model) -> None:
    version, _, _ = model
    compiled = _compile(db_session, version, Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
        dimension_phrases=["country"],
    ))

    assert "JOIN   public.customers AS e1 ON e0.customer_id = e1.id" in compiled.sql
    assert "GROUP BY e1.country" in compiled.sql
    assert compiled.columns == ["country", "revenue"]


def test_division_is_guarded(db_session: Session, model) -> None:
    """A quarter with no orders must not raise division by zero."""
    version, _, _ = model
    compiled = _compile(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["average order value"]))
    assert "NULLIF(COUNT(DISTINCT e0.id), 0)" in compiled.sql


def test_values_are_bound_never_interpolated(db_session: Session, model) -> None:
    version, _, _ = model
    compiled = _compile(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        filters=[FilterPhrase(field_phrase="country", operator=FilterOperatorPhrase.EQ,
                              value_phrases=["Texas"])],
    ))
    assert "Texas" not in compiled.sql
    assert "Texas" in compiled.parameters.values()


def test_a_time_range_binds_two_dates(db_session: Session, model) -> None:
    version, _, _ = model
    compiled = _compile(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        time_range=TimeRangePhrase(phrase="last quarter"),
    ))
    assert ">=" in compiled.sql and "<" in compiled.sql
    assert date(2026, 4, 1) in compiled.parameters.values()
    assert date(2026, 7, 1) in compiled.parameters.values()


def test_business_rules_are_injected_without_being_asked_for(
    db_session: Session, model
) -> None:
    version, _, _ = model
    compiled = _compile(db_session, version, Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
        dimension_phrases=["country"],
    ))
    assert "e1.is_internal" in compiled.sql
    assert False in compiled.parameters.values()


def test_a_time_grain_buckets_and_groups(db_session: Session, model) -> None:
    version, _, _ = model
    compiled = _compile(db_session, version, Intent(
        intent_type=IntentType.TREND, metric_phrases=["revenue"],
        time_range=TimeRangePhrase(phrase="last quarter"),
    ))
    assert "date_trunc('quarter', e0.order_date)" in compiled.sql


def test_compilation_is_deterministic(db_session: Session, model) -> None:
    version, _, _ = model
    intent = Intent(intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
                    dimension_phrases=["country"],
                    time_range=TimeRangePhrase(phrase="last quarter"))
    first = _compile(db_session, version, intent)
    second = _compile(db_session, version, intent)
    assert first.sql == second.sql
    assert list(first.parameters.values()) == list(second.parameters.values())


def test_the_statement_is_a_select_with_no_statement_terminator(
    db_session: Session, model
) -> None:
    version, _, _ = model
    compiled = _compile(db_session, version,
                        Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]))
    assert ";" not in compiled.sql
    assert "--" not in compiled.sql
    assert compiled.sql.count("SELECT") == 1


def test_a_relationship_without_join_keys_is_refused(db_session: Session, model) -> None:
    """A JOIN with no ON clause is a cartesian product wearing a disguise."""
    version, order, customer = model
    db_session.execute(text("DELETE FROM relationship_join_keys"))
    db_session.flush()

    with pytest.raises(CompilationError) as exc:
        _compile(db_session, version, Intent(
            intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
            dimension_phrases=["country"]))
    assert "join keys" in str(exc.value)


def test_the_row_limit_is_emitted(db_session: Session, model) -> None:
    version, _, _ = model
    compiled = _compile(db_session, version,
                        Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]))
    assert "LIMIT" in compiled.sql


def test_a_rule_entity_is_joined_even_when_unmentioned(db_session: Session, model) -> None:
    """A rule on customers must apply to `revenue`, not only to `revenue by country`.
    Otherwise the same question is answered two different ways depending on
    whether it happens to touch the table the rule lives on."""
    version, _, _ = model
    compiled = _compile(db_session, version,
                        Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]))

    assert "public.customers" in compiled.sql
    assert "is_internal" in compiled.sql


def test_a_fan_out_join_is_refused(db_session: Session, model) -> None:
    """Walking one-to-many repeats every row on the other side, silently
    double-counting every aggregate."""
    version, order, customer = model
    db_session.execute(
        text("UPDATE relationships SET cardinality = 'one_to_many'")
    )
    db_session.expire_all()

    with pytest.raises(CompilationError) as exc:
        _compile(db_session, version, Intent(
            intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
            dimension_phrases=["country"]))
    assert "double-counts" in str(exc.value)
