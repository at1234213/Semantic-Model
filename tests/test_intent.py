"""Step 24: intent understanding and deterministic phrase resolution."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    Aggregation,
    Dimension,
    Entity,
    GlossaryTerm,
    Measure,
    Metric,
    SearchObjectType,
    SemanticModel,
    SemanticModelVersion,
    Synonym,
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
from app.services.resolution import resolve
from app.services.retrieval import rebuild_search_index


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _semantic_model(db: Session) -> SemanticModelVersion:
    tenant = Tenant(name="acme")
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

    order = Entity(tenant_id=tenant.id, semantic_model_version_id=version.id,
                   name="order", source_table="orders")
    customer = Entity(tenant_id=tenant.id, semantic_model_version_id=version.id,
                      name="customer", source_table="customers")
    db.add_all([order, customer])
    db.flush()

    db.add_all([
        Dimension(tenant_id=tenant.id, entity_id=customer.id, name="country",
                  expression="country", description="Billing country of the customer"),
        Dimension(tenant_id=tenant.id, entity_id=customer.id, name="industry",
                  expression="industry", description="The sector a customer operates in"),
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
    db.add(Synonym(tenant_id=tenant.id, semantic_model_version_id=version.id,
                   term="turnover", metric_id=revenue.id))
    db.add(GlossaryTerm(tenant_id=tenant.id, semantic_model_version_id=version.id,
                        term="active customer",
                        definition="A customer with an order in the last 90 days"))
    db.flush()

    rebuild_search_index(db, version.id)
    return version


# ---------- the parser ----------


def test_scripted_parser_returns_registered_intents() -> None:
    parser = ScriptedIntentParser()
    expected = Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"])
    parser.register("what was revenue?", expected)
    assert parser.parse("What Was Revenue?") == expected


def test_unregistered_questions_are_unsupported_not_guessed() -> None:
    """Failing loudly beats drifting: a forgotten registration must not pass."""
    parser = ScriptedIntentParser()
    result = parser.parse("something nobody registered")
    assert result.intent_type is IntentType.UNSUPPORTED
    assert result.note


def test_an_intent_carries_no_identifiers() -> None:
    """The model returns phrases; resolution to objects happens elsewhere."""
    fields = set(Intent.model_fields)
    assert not any("id" in name for name in fields)


@pytest.mark.parametrize(
    "intent,answerable",
    [
        (Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]), True),
        (Intent(intent_type=IntentType.AGGREGATE), False),  # no metric to compute
        (Intent(intent_type=IntentType.DEFINITION), True),  # needs no metric
        (Intent(intent_type=IntentType.UNSUPPORTED, note="why"), False),
    ],
)
def test_answerability(intent: Intent, answerable: bool) -> None:
    assert intent.is_answerable is answerable


# ---------- resolution ----------


def test_a_metric_phrase_resolves_to_a_metric(db_session: Session) -> None:
    version = _semantic_model(db_session)
    intent = Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"])

    resolved = resolve(db_session, intent, version_id=version.id)
    assert resolved.is_complete
    assert resolved.metrics[0].hit.name == "revenue"
    assert resolved.metrics[0].hit.object_type is SearchObjectType.METRIC


def test_a_synonym_resolves_to_its_metric(db_session: Session) -> None:
    version = _semantic_model(db_session)
    intent = Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["turnover"])

    resolved = resolve(db_session, intent, version_id=version.id)
    assert resolved.is_complete
    assert resolved.metrics[0].hit.name == "revenue"


def test_a_breakdown_resolves_metric_and_dimension(db_session: Session) -> None:
    version = _semantic_model(db_session)
    intent = Intent(
        intent_type=IntentType.BREAKDOWN,
        metric_phrases=["revenue"],
        dimension_phrases=["country"],
        time_range=TimeRangePhrase(phrase="last quarter", grain="quarter"),
    )

    resolved = resolve(db_session, intent, version_id=version.id)
    assert resolved.is_complete
    assert resolved.dimensions[0].hit.name == "country"


def test_filter_fields_are_resolved_too(db_session: Session) -> None:
    version = _semantic_model(db_session)
    intent = Intent(
        intent_type=IntentType.AGGREGATE,
        metric_phrases=["revenue"],
        filters=[
            FilterPhrase(field_phrase="industry", operator=FilterOperatorPhrase.EQ,
                         value_phrases=["retail"])
        ],
    )

    resolved = resolve(db_session, intent, version_id=version.id)
    assert resolved.is_complete
    assert resolved.filter_fields[0].hit.name == "industry"


def test_an_unknown_phrase_is_reported_not_guessed(db_session: Session) -> None:
    version = _semantic_model(db_session)
    intent = Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["gross wombats"])

    resolved = resolve(db_session, intent, version_id=version.id)
    assert not resolved.is_complete
    assert "gross wombats" in resolved.problems[0]


def test_the_wrong_kind_of_match_is_a_failure_with_a_useful_message(
    db_session: Session,
) -> None:
    """'country' exists, but as a dimension. Asked for as a metric, that is an error."""
    version = _semantic_model(db_session)
    intent = Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["country"])

    resolved = resolve(db_session, intent, version_id=version.id)
    assert not resolved.is_complete
    problem = resolved.problems[0]
    assert "country" in problem and "dimension" in problem and "metric" in problem


def test_an_unsupported_intent_resolves_to_its_own_reason(db_session: Session) -> None:
    version = _semantic_model(db_session)
    intent = Intent(intent_type=IntentType.UNSUPPORTED, note="Asks about the weather.")

    resolved = resolve(db_session, intent, version_id=version.id)
    assert not resolved.is_complete
    assert resolved.problems == ["Asks about the weather."]
    assert resolved.metrics == []  # no lookups attempted


def test_resolution_is_deterministic(db_session: Session) -> None:
    version = _semantic_model(db_session)
    intent = Intent(intent_type=IntentType.BREAKDOWN, metric_phrases=["revenue"],
                    dimension_phrases=["country"])

    first = resolve(db_session, intent, version_id=version.id)
    second = resolve(db_session, intent, version_id=version.id)
    assert [r.hit.object_id for r in first.all_resolutions] == [
        r.hit.object_id for r in second.all_resolutions
    ]


def test_resolution_is_scoped_to_the_given_version(db_session: Session) -> None:
    version = _semantic_model(db_session)
    other = SemanticModelVersion(
        tenant_id=version.tenant_id, semantic_model_id=version.semantic_model_id,
        version=2, status=VersionStatus.DRAFT,
    )
    db_session.add(other)
    db_session.flush()

    intent = Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"])
    assert not resolve(db_session, intent, version_id=other.id).is_complete


def test_end_to_end_question_to_resolved_objects(db_session: Session) -> None:
    version = _semantic_model(db_session)

    parser = ScriptedIntentParser()
    parser.register(
        "what was turnover by country last quarter?",
        Intent(
            intent_type=IntentType.BREAKDOWN,
            metric_phrases=["turnover"],
            dimension_phrases=["country"],
            time_range=TimeRangePhrase(phrase="last quarter", grain="quarter"),
        ),
    )

    intent = parser.parse("What was turnover by country last quarter?")
    resolved = resolve(db_session, intent, version_id=version.id)

    assert resolved.is_complete
    assert resolved.metrics[0].hit.name == "revenue"
    assert resolved.dimensions[0].hit.name == "country"
    assert resolved.intent.time_range.grain == "quarter"
