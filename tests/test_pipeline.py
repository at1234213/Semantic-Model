"""Step 25: the orchestration graph and its repair loop."""

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
from app.services.intent import (
    FilterOperatorPhrase,
    FilterPhrase,
    Intent,
    IntentType,
    ScriptedIntentParser,
    TimeRangePhrase,
)
from app.services.metrics import set_expression
from app.services.pipeline import answer, build_pipeline
from app.services.retrieval import rebuild_search_index

TODAY = date(2026, 9, 2)


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


@pytest.fixture
def model(db_session: Session) -> SemanticModelVersion:
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
        Dimension(tenant_id=tenant.id, entity_id=customer.id, name="seats",
                  expression="seats", data_type=DimensionDataType.NUMBER,
                  description="Licensed seats"),
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


# ---------- the happy path ----------


def test_a_question_compiles_end_to_end(db_session: Session, model) -> None:
    parser = ScriptedIntentParser()
    parser.register("revenue by country last quarter", Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
        dimension_phrases=["country"],
        time_range=TimeRangePhrase(phrase="last quarter"),
    ))

    result = answer(db_session, "revenue by country last quarter",
                    version_id=model.id, today=TODAY, parser=parser)

    assert result.ok
    assert result.attempts == 1
    assert "SUM(e0.amount)" in result.compiled.sql
    assert "e1.country" in result.compiled.sql
    assert result.problems == []


def test_the_graph_has_the_expected_nodes(db_session: Session) -> None:
    pipeline = build_pipeline(db_session, parser=ScriptedIntentParser())
    nodes = set(pipeline.get_graph().nodes)
    assert {"understand_intent", "resolve_phrases", "build_plan",
            "repair_intent", "compile_sql", "give_up"} <= nodes


# ---------- the repair loop ----------


def test_a_bad_first_attempt_is_repaired(db_session: Session, model) -> None:
    """The problems from a failed plan go back to the parser as feedback."""
    question = "how much did we make by country"
    parser = ScriptedIntentParser()
    # First attempt names a metric the model does not define.
    parser.register(question, Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["takings"],
        dimension_phrases=["country"],
    ))
    # Told what was wrong, it corrects itself.
    parser.register(
        question,
        Intent(intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
               dimension_phrases=["country"]),
        feedback=["Nothing in this semantic model matches 'takings'"],
    )

    result = answer(db_session, question, version_id=model.id, today=TODAY, parser=parser)

    assert result.ok, result.problems
    assert result.attempts == 2
    assert result.intent.metric_phrases == ["revenue"]


def test_repair_is_bounded(db_session: Session, model) -> None:
    """A parser that cannot fix itself must not loop forever."""
    question = "something unanswerable"
    parser = ScriptedIntentParser()
    stubborn = Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["wombats"])
    parser.register(question, stubborn)
    parser.register(question, stubborn,
                    feedback=["Nothing in this semantic model matches 'wombats'"])

    result = answer(db_session, question, version_id=model.id, today=TODAY,
                    parser=parser, max_attempts=2)

    assert not result.ok
    assert result.attempts == 2
    assert result.problems


def test_an_unsupported_intent_is_not_retried(db_session: Session, model) -> None:
    """Rephrasing cannot help a question the model correctly declined."""
    question = "what is the weather in Oslo"
    parser = ScriptedIntentParser()
    parser.register(question, Intent(intent_type=IntentType.UNSUPPORTED,
                                     note="Not answerable from a semantic model."))

    result = answer(db_session, question, version_id=model.id, today=TODAY, parser=parser)

    assert not result.ok
    assert result.attempts == 1  # no repair attempted
    assert "Not answerable" in result.problems[0]


def test_a_type_error_is_repairable(db_session: Session, model) -> None:
    question = "revenue for big customers"
    parser = ScriptedIntentParser()
    parser.register(question, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        filters=[FilterPhrase(field_phrase="seats", operator=FilterOperatorPhrase.GT,
                              value_phrases=["lots"])],
    ))
    problems = [
        "'seats' holds number values, but the question compares it to something "
        "else: invalid literal for int() with base 10: 'lots'"
    ]
    parser.register(question, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        filters=[FilterPhrase(field_phrase="seats", operator=FilterOperatorPhrase.GT,
                              value_phrases=["100"])],
    ), feedback=problems)

    result = answer(db_session, question, version_id=model.id, today=TODAY, parser=parser)
    assert result.ok, result.problems
    assert 100 in result.compiled.parameters.values()


# ---------- failure that repair cannot fix ----------


def test_a_broken_model_fails_without_retrying(db_session: Session, model) -> None:
    """An incomplete semantic model is not the question's fault."""
    db_session.execute(text("DELETE FROM relationship_join_keys"))
    db_session.flush()

    parser = ScriptedIntentParser()
    parser.register("revenue by country", Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
        dimension_phrases=["country"]))

    result = answer(db_session, "revenue by country", version_id=model.id,
                    today=TODAY, parser=parser)

    assert not result.ok
    assert "join keys" in result.problems[0]
    assert result.attempts == 1


def test_business_rules_survive_the_pipeline(db_session: Session, model) -> None:
    parser = ScriptedIntentParser()
    parser.register("revenue by country", Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
        dimension_phrases=["country"]))

    result = answer(db_session, "revenue by country", version_id=model.id,
                    today=TODAY, parser=parser)
    assert result.ok
    assert "e1.is_internal" in result.compiled.sql


def test_the_pipeline_is_deterministic(db_session: Session, model) -> None:
    parser = ScriptedIntentParser()
    parser.register("revenue by country", Intent(
        intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
        dimension_phrases=["country"]))

    first = answer(db_session, "revenue by country", version_id=model.id,
                   today=TODAY, parser=parser)
    second = answer(db_session, "revenue by country", version_id=model.id,
                    today=TODAY, parser=parser)
    assert first.compiled.sql == second.compiled.sql
