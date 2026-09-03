"""Step 28: running compiled SQL against a real warehouse, and reading results."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_secret
from app.models import (
    Aggregation,
    BusinessRule,
    Cardinality,
    DataSource,
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
from app.services.analysis import analyse
from app.services.compiler import CompiledQuery
from app.services.execution import execute
from app.services.intent import Intent, IntentType, ScriptedIntentParser, TimeRangePhrase
from app.services.metrics import set_expression
from app.services.pipeline import answer
from app.services.retrieval import rebuild_search_index

TODAY = date(2026, 9, 2)  # Q2 2026 is "last quarter"


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


@pytest.fixture
def model(db_session: Session, warehouse_tables: str) -> SemanticModelVersion:
    """A semantic model pointed at the real warehouse schema."""
    db = db_session
    tenant = Tenant(name="acme")
    db.add(tenant)
    db.flush()
    _scope(db, tenant.id)

    workspace = Workspace(tenant_id=tenant.id, name="analytics")
    db.add(workspace)
    db.flush()

    ciphertext, key_version = encrypt_secret("postgres")
    source = DataSource(
        tenant_id=tenant.id, workspace_id=workspace.id, name="warehouse",
        host="db", database="semantic_model", username="postgres",
        password_ciphertext=ciphertext, key_version=key_version,
        default_schema=warehouse_tables,
    )
    db.add(source)
    db.flush()

    sm = SemanticModel(tenant_id=tenant.id, workspace_id=workspace.id, name="Sales")
    db.add(sm)
    db.flush()
    version = SemanticModelVersion(
        tenant_id=tenant.id, semantic_model_id=sm.id, version=1,
        status=VersionStatus.DRAFT, data_source_id=source.id,
    )
    db.add(version)
    db.flush()

    order = Entity(tenant_id=tenant.id, semantic_model_version_id=version.id,
                   name="order", source_schema=warehouse_tables, source_table="orders")
    customer = Entity(tenant_id=tenant.id, semantic_model_version_id=version.id,
                      name="customer", source_schema=warehouse_tables,
                      source_table="customers")
    db.add_all([order, customer])
    db.flush()

    edge = Relationship(tenant_id=tenant.id, semantic_model_version_id=version.id,
                        from_entity_id=order.id, to_entity_id=customer.id,
                        name="order_to_customer", cardinality=Cardinality.MANY_TO_ONE)
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
    db.add(Measure(tenant_id=tenant.id, entity_id=order.id,
                   semantic_model_version_id=version.id, name="gross_revenue",
                   aggregation=Aggregation.SUM, expression="amount"))
    db.flush()

    revenue = Metric(tenant_id=tenant.id, semantic_model_version_id=version.id,
                     name="revenue", expression="${gross_revenue}",
                     description="Total money billed")
    db.add(revenue)
    db.flush()
    set_expression(db, revenue, "${gross_revenue}")
    db.add(BusinessRule(tenant_id=tenant.id, semantic_model_version_id=version.id,
                        entity_id=customer.id, name="exclude_internal",
                        column_name="is_internal", operator=FilterOperator.EQ, value=False))
    db.flush()
    rebuild_search_index(db, version.id)
    return version


def _ask(db: Session, version, question: str, intent: Intent):
    parser = ScriptedIntentParser()
    parser.register(question, intent)
    return answer(db, question, version_id=version.id, today=TODAY,
                  parser=parser, execute_query=True)


# ---------- real numbers ----------


def test_a_question_returns_the_right_numbers(db_session: Session, model) -> None:
    """US has orders of 100 and 250 in Q2; CA has 50. The 999 order belongs to
    an internal customer and the 10 order falls outside the quarter."""
    result = _ask(db_session, model, "revenue by country last quarter", Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
        dimension_phrases=["country"],
        time_range=TimeRangePhrase(phrase="last quarter"),
    ))

    assert result.ok, result.problems
    by_country = {row[result.compiled.columns.index("country")]:
                  row[result.compiled.columns.index("revenue")]
                  for row in result.result.rows}
    assert by_country == {"US": Decimal("350.00"), "CA": Decimal("50.00")}


def test_the_business_rule_actually_excludes_rows(db_session: Session, model) -> None:
    """Without it the 999 internal order would appear in the total."""
    result = _ask(db_session, model, "revenue last quarter", Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        time_range=TimeRangePhrase(phrase="last quarter"),
    ))
    assert result.ok, result.problems
    total = result.result.rows[0][result.compiled.columns.index("revenue")]
    assert total == Decimal("400.00")  # 100 + 250 + 50, not 1399


def test_the_time_range_actually_excludes_rows(db_session: Session, model) -> None:
    """The January order is outside the quarter."""
    with_range = _ask(db_session, model, "revenue last quarter", Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        time_range=TimeRangePhrase(phrase="last quarter")))
    without = _ask(db_session, model, "revenue", Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]))

    def revenue(result):
        return result.result.rows[0][result.compiled.columns.index("revenue")]

    assert revenue(with_range) == Decimal("400.00")
    assert revenue(without) == Decimal("410.00")  # the January order returns


def test_an_empty_result_is_not_an_error(db_session: Session, model) -> None:
    """Grouped by a time bucket, a range with no orders yields no rows at all."""
    result = _ask(db_session, model, "revenue yesterday", Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        time_range=TimeRangePhrase(phrase="yesterday")))
    assert result.ok
    assert result.analysis.is_empty
    assert result.analysis.headline() == "No rows matched."


# ---------- safety ----------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO warehouse.orders VALUES (99, 1, 1, '2026-01-01')",
        "UPDATE warehouse.orders SET amount = 0",
        "DELETE FROM warehouse.orders",
        "DROP TABLE warehouse.orders",
        "SELECT 1; DROP TABLE warehouse.orders",
    ],
)
def test_execute_refuses_anything_that_is_not_a_bare_select(
    db_session: Session, model, statement: str
) -> None:
    """Layer 2 of four: the text is re-checked immediately before execution, so a
    statement that reached here some other way still never runs. Layer 3 — the
    warehouse's own read-only transaction — is covered in test_data_sources.py."""
    from app.services.execution import data_source_for

    source = data_source_for(db_session, model.id)
    with pytest.raises(Exception) as exc:
        execute(source, CompiledQuery(sql=statement))
    assert "SELECT" in str(exc.value) or "contains" in str(exc.value)


def test_a_legitimate_select_runs(db_session: Session, model) -> None:
    from app.services.execution import data_source_for

    source = data_source_for(db_session, model.id)
    result = execute(source, CompiledQuery(sql="SELECT 1 AS one"))
    assert result.rows == [(1,)]
    assert result.duration_ms >= 0


def test_execution_needs_a_data_source(db_session: Session, model) -> None:
    model.data_source_id = None
    db_session.flush()

    result = _ask(db_session, model, "revenue", Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]))
    assert not result.ok
    assert "not connected to a data source" in result.problems[0]


def test_compiling_without_executing_needs_no_warehouse(db_session: Session, model) -> None:
    parser = ScriptedIntentParser()
    parser.register("revenue", Intent(intent_type=IntentType.AGGREGATE,
                                      metric_phrases=["revenue"]))
    result = answer(db_session, "revenue", version_id=model.id, today=TODAY,
                    parser=parser, execute_query=False)
    assert result.ok
    assert result.compiled is not None
    assert result.result is None


# ---------- analysis ----------


def test_analysis_summarises_numeric_and_categorical_columns(
    db_session: Session, model
) -> None:
    result = _ask(db_session, model, "revenue by country last quarter", Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
        dimension_phrases=["country"],
        time_range=TimeRangePhrase(phrase="last quarter")))

    analysis = result.analysis
    by_name = {c.name: c for c in analysis.columns}
    assert by_name["revenue"].kind == "numeric"
    assert by_name["revenue"].total == 400.0
    assert by_name["country"].kind == "categorical"
    assert by_name["country"].distinct_count == 2


def test_the_headline_states_what_came_back(db_session: Session, model) -> None:
    result = _ask(db_session, model, "revenue by country last quarter", Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
        dimension_phrases=["country"],
        time_range=TimeRangePhrase(phrase="last quarter")))
    assert "2 rows" in result.analysis.headline()


def test_analysis_of_an_empty_result() -> None:
    from app.services.execution import QueryResult

    analysis = analyse(QueryResult(columns=["revenue"], rows=[]))
    assert analysis.is_empty
    assert analysis.headline() == "No rows matched."


def test_truncation_is_reported(db_session: Session, model) -> None:
    from app.services.execution import data_source_for

    source = data_source_for(db_session, model.id)
    result = execute(
        source,
        CompiledQuery(sql="SELECT id FROM warehouse.orders ORDER BY id", parameters={}),
        max_rows=2,
    )
    assert result.truncated
    assert result.row_count == 2
    assert "(truncated)" in analyse(result).headline()
