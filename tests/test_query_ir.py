"""Step 26: time resolution and the Semantic Query IR."""

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    Aggregation,
    Dimension,
    DimensionDataType,
    Entity,
    Measure,
    Metric,
    SemanticModel,
    SemanticModelVersion,
    Tenant,
    TimeGranularity,
    VersionStatus,
    Workspace,
)
from app.services.intent import (
    FilterOperatorPhrase,
    FilterPhrase,
    Intent,
    IntentType,
    TimeRangePhrase,
)
from app.services.query_ir import MAX_ROW_LIMIT, build
from app.services.resolution import resolve
from app.services.retrieval import rebuild_search_index
from app.services.timeframe import TimeframeError, resolve_timeframe

TODAY = date(2026, 9, 2)  # a Wednesday in Q3


# ---------- timeframe ----------


@pytest.mark.parametrize(
    "phrase,start,end",
    [
        ("last quarter", date(2026, 4, 1), date(2026, 7, 1)),
        ("this quarter", date(2026, 7, 1), date(2026, 10, 1)),
        ("last month", date(2026, 8, 1), date(2026, 9, 1)),
        ("this month", date(2026, 9, 1), date(2026, 10, 1)),
        ("last year", date(2025, 1, 1), date(2026, 1, 1)),
        ("yesterday", date(2026, 9, 1), date(2026, 9, 2)),
        ("last 7 days", date(2026, 8, 27), date(2026, 9, 3)),
        ("last 2 months", date(2026, 7, 1), date(2026, 9, 3)),
    ],
)
def test_phrases_resolve_to_expected_dates(phrase: str, start: date, end: date) -> None:
    resolved = resolve_timeframe(phrase, today=TODAY)
    assert (resolved.start, resolved.end) == (start, end)


def test_ranges_are_half_open() -> None:
    """end is exclusive, so >= start AND < end is correct for dates and timestamps."""
    september = resolve_timeframe("this month", today=TODAY)
    august = resolve_timeframe("last month", today=TODAY)
    assert august.end == september.start


def test_year_to_date_includes_today() -> None:
    ytd = resolve_timeframe("year to date", today=TODAY)
    assert ytd.start == date(2026, 1, 1)
    assert ytd.end > TODAY


def test_phrases_are_case_and_space_insensitive() -> None:
    assert resolve_timeframe("  LAST   Quarter ", today=TODAY) == resolve_timeframe(
        "last quarter", today=TODAY
    )


@pytest.mark.parametrize("phrase", ["", "   ", "when pigs fly", "last 0 days", "soonish"])
def test_unresolvable_phrases_raise_rather_than_guess(phrase: str) -> None:
    with pytest.raises(TimeframeError):
        resolve_timeframe(phrase, today=TODAY)


# ---------- fixtures ----------


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _model(db: Session, *, time_dimensions: int = 1,
           granularity: TimeGranularity | None = None) -> SemanticModelVersion:
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
                  expression="country", description="Billing country"),
        Dimension(tenant_id=tenant.id, entity_id=customer.id, name="seats",
                  expression="seats", data_type=DimensionDataType.NUMBER,
                  description="Number of licensed seats"),
        Dimension(tenant_id=tenant.id, entity_id=customer.id, name="is_internal",
                  expression="is_internal", data_type=DimensionDataType.BOOLEAN,
                  description="Whether this is one of our own test accounts"),
    ])
    for index in range(time_dimensions):
        db.add(
            Dimension(
                tenant_id=tenant.id, entity_id=order.id,
                name=f"order_date{'' if index == 0 else index}",
                expression="order_date", data_type=DimensionDataType.DATE,
                granularity=granularity, description="When the order was placed",
            )
        )
    db.flush()

    db.add(Measure(tenant_id=tenant.id, entity_id=order.id,
                   semantic_model_version_id=version.id, name="gross_revenue",
                   aggregation=Aggregation.SUM, expression="amount"))
    db.flush()
    db.add(Metric(tenant_id=tenant.id, semantic_model_version_id=version.id,
                  name="revenue", expression="${gross_revenue}",
                  description="Total money billed"))
    db.flush()
    rebuild_search_index(db, version.id)
    return version


def _build(db: Session, version: SemanticModelVersion, intent: Intent):
    return build(db, resolve(db, intent, version_id=version.id),
                 version_id=version.id, today=TODAY)


# ---------- building the IR ----------


def test_a_simple_aggregate_builds(db_session: Session) -> None:
    version = _model(db_session)
    result = _build(db_session, version,
                    Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]))

    assert result.ok
    assert len(result.query.metric_ids) == 1
    assert result.query.time_range is None


def test_a_breakdown_with_a_time_range_builds(db_session: Session) -> None:
    version = _model(db_session)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.BREAKDOWN,
        metric_phrases=["revenue"],
        dimension_phrases=["country"],
        time_range=TimeRangePhrase(phrase="last quarter"),
    ))

    assert result.ok, result.problems
    assert result.query.time_range.start == date(2026, 4, 1)
    assert result.query.time_dimension_id is not None


def test_the_ir_contains_ids_not_phrases(db_session: Session) -> None:
    version = _model(db_session)
    result = _build(db_session, version,
                    Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]))
    assert all(isinstance(value, uuid.UUID) for value in result.query.metric_ids)


@pytest.mark.parametrize(
    "dimension,value,expected",
    [("seats", "50", 50), ("is_internal", "false", False), ("country", "Texas", "Texas")],
)
def test_filter_values_are_typed_from_the_dimension(
    db_session: Session, dimension: str, value: str, expected: object
) -> None:
    version = _model(db_session)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        filters=[FilterPhrase(field_phrase=dimension,
                              operator=FilterOperatorPhrase.EQ, value_phrases=[value])],
    ))

    assert result.ok, result.problems
    assert result.query.filters[0].values == [expected]


def test_a_value_of_the_wrong_type_is_reported(db_session: Session) -> None:
    version = _model(db_session)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        filters=[FilterPhrase(field_phrase="seats", operator=FilterOperatorPhrase.EQ,
                              value_phrases=["a lot"])],
    ))

    assert not result.ok
    assert "number" in result.problems[0]


def test_a_scalar_operator_with_several_values_is_reported(db_session: Session) -> None:
    version = _model(db_session)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        filters=[FilterPhrase(field_phrase="country", operator=FilterOperatorPhrase.EQ,
                              value_phrases=["Texas", "Utah"])],
    ))
    assert not result.ok
    assert "takes one" in result.problems[0]


def test_in_accepts_several_values(db_session: Session) -> None:
    version = _model(db_session)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        filters=[FilterPhrase(field_phrase="country", operator=FilterOperatorPhrase.IN,
                              value_phrases=["Texas", "Utah"])],
    ))
    assert result.ok, result.problems
    assert result.query.filters[0].values == ["Texas", "Utah"]


def test_every_problem_is_reported_not_just_the_first(db_session: Session) -> None:
    """Someone fixing a question wants the whole list."""
    version = _model(db_session)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE,
        metric_phrases=["revenue"],
        filters=[
            FilterPhrase(field_phrase="seats", operator=FilterOperatorPhrase.EQ,
                         value_phrases=["heaps"]),
            FilterPhrase(field_phrase="is_internal", operator=FilterOperatorPhrase.EQ,
                         value_phrases=["maybe"]),
        ],
    ))
    assert len(result.problems) == 2


def test_an_unresolvable_time_phrase_is_reported(db_session: Session) -> None:
    version = _model(db_session)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        time_range=TimeRangePhrase(phrase="around harvest time"),
    ))
    assert not result.ok
    assert "not a time range" in result.problems[0]


def test_ambiguous_time_dimensions_are_reported_not_picked(db_session: Session) -> None:
    """Answering against the wrong date column is wrong in a way nobody notices."""
    version = _model(db_session, time_dimensions=2)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        time_range=TimeRangePhrase(phrase="last quarter"),
    ))
    assert not result.ok
    assert "several date dimensions" in result.problems[0]


def test_no_time_dimension_at_all_is_reported(db_session: Session) -> None:
    version = _model(db_session, time_dimensions=0)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        time_range=TimeRangePhrase(phrase="last quarter"),
    ))
    assert not result.ok
    assert "no date or timestamp dimension" in result.problems[0]


def test_asking_for_a_finer_grain_than_recorded_is_reported(db_session: Session) -> None:
    version = _model(db_session, granularity=TimeGranularity.MONTH)
    result = _build(db_session, version, Intent(
        intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"],
        time_range=TimeRangePhrase(phrase="yesterday"),
    ))
    assert not result.ok
    assert "only recorded to the month" in result.problems[0]


def test_unresolved_phrases_stop_the_build(db_session: Session) -> None:
    version = _model(db_session)
    result = _build(db_session, version,
                    Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["wombats"]))
    assert result.query is None
    assert result.problems


def test_the_row_limit_is_capped(db_session: Session) -> None:
    version = _model(db_session)
    resolved = resolve(db_session,
                       Intent(intent_type=IntentType.AGGREGATE, metric_phrases=["revenue"]),
                       version_id=version.id)
    result = build(db_session, resolved, version_id=version.id, today=TODAY,
                   limit=10_000_000)
    assert result.query.limit == MAX_ROW_LIMIT
