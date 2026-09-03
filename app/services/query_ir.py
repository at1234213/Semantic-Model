"""The Semantic Query IR — what the compiler consumes.

Everything here is resolved: metrics and dimensions are database IDs, filter
values are typed Python objects, and the time range is two concrete dates. No
phrases, no SQL, nothing a model chose directly.

The IR is built from a ResolvedIntent by `build`, which is deterministic and
reports every problem it finds rather than raising on the first one — a person
fixing a question wants the whole list.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dimension import Dimension, DimensionDataType, TimeGranularity
from app.models.metric import Metric
from app.services.intent import FilterOperatorPhrase
from app.services.resolution import ResolvedIntent
from app.services.timeframe import TimeframeError, TimeRange, resolve_timeframe

MAX_ROW_LIMIT = 10_000
DEFAULT_ROW_LIMIT = 1_000

# Operators taking a list rather than a scalar.
SET_OPERATORS = (FilterOperatorPhrase.IN, FilterOperatorPhrase.NOT_IN)


@dataclass
class QueryFilter:
    dimension_id: uuid.UUID
    operator: FilterOperatorPhrase
    values: list[object]


@dataclass
class SemanticQuery:
    version_id: uuid.UUID
    metric_ids: list[uuid.UUID] = field(default_factory=list)
    dimension_ids: list[uuid.UUID] = field(default_factory=list)
    filters: list[QueryFilter] = field(default_factory=list)
    time_dimension_id: uuid.UUID | None = None
    time_range: TimeRange | None = None
    grain: TimeGranularity | None = None
    limit: int = DEFAULT_ROW_LIMIT


@dataclass
class BuildResult:
    query: SemanticQuery | None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.query is not None and not self.problems


def _coerce(value: str, data_type: DimensionDataType) -> object:
    """Turn a filter value phrase into a typed Python object.

    Raises ValueError on a mismatch, which becomes a reported problem rather
    than a value silently compared as the wrong type.
    """
    text = value.strip()
    if data_type is DimensionDataType.NUMBER:
        return float(text) if "." in text else int(text)
    if data_type is DimensionDataType.BOOLEAN:
        lowered = text.lower()
        if lowered in ("true", "yes", "y", "1"):
            return True
        if lowered in ("false", "no", "n", "0"):
            return False
        raise ValueError(f"{value!r} is not a yes/no value")
    if data_type is DimensionDataType.DATE:
        return date.fromisoformat(text)
    if data_type is DimensionDataType.TIMESTAMP:
        return datetime.fromisoformat(text)
    return text


def _pick_time_dimension(
    db: Session, version_id: uuid.UUID, problems: list[str]
) -> Dimension | None:
    """Find the one temporal dimension to filter on.

    Ambiguity is reported, never resolved by picking the first. A question
    answered against the wrong date column is wrong in a way nobody notices.
    """
    candidates = list(
        db.scalars(
            select(Dimension)
            .join(Dimension.entity)
            .where(
                Dimension.data_type.in_(
                    [DimensionDataType.DATE, DimensionDataType.TIMESTAMP]
                )
            )
        )
    )
    candidates = [
        dimension
        for dimension in candidates
        if dimension.entity.semantic_model_version_id == version_id
    ]

    if not candidates:
        problems.append(
            "This question asks for a time range, but the model defines no date "
            "or timestamp dimension to filter on."
        )
        return None
    if len(candidates) > 1:
        names = ", ".join(sorted(d.name for d in candidates))
        problems.append(
            f"This question asks for a time range, but several date dimensions "
            f"could be meant ({names}). Name the one you want."
        )
        return None
    return candidates[0]


def build(
    db: Session,
    resolved: ResolvedIntent,
    *,
    version_id: uuid.UUID,
    today: date | None = None,
    limit: int = DEFAULT_ROW_LIMIT,
) -> BuildResult:
    """Assemble a SemanticQuery, collecting every problem rather than the first."""
    problems: list[str] = list(resolved.problems)
    if problems:
        return BuildResult(query=None, problems=problems)

    query = SemanticQuery(
        version_id=version_id,
        metric_ids=[r.hit.object_id for r in resolved.metrics],
        dimension_ids=[r.hit.object_id for r in resolved.dimensions],
        limit=max(1, min(limit, MAX_ROW_LIMIT)),
    )

    dimensions_by_id = {
        dimension.id: dimension
        for dimension in db.scalars(
            select(Dimension).where(
                Dimension.id.in_(
                    [r.hit.object_id for r in resolved.dimensions + resolved.filter_fields]
                )
            )
        )
    }

    for resolution, filter_phrase in zip(
        resolved.filter_fields, resolved.intent.filters, strict=True
    ):
        dimension = dimensions_by_id.get(resolution.hit.object_id)
        if dimension is None:
            problems.append(f"{resolution.phrase!r} could not be loaded")
            continue

        if not filter_phrase.value_phrases:
            problems.append(f"The filter on {dimension.name!r} has no value to compare")
            continue
        if (
            filter_phrase.operator not in SET_OPERATORS
            and len(filter_phrase.value_phrases) > 1
        ):
            problems.append(
                f"The filter on {dimension.name!r} has several values but uses "
                f"{filter_phrase.operator}, which takes one"
            )
            continue

        try:
            values = [
                _coerce(value, dimension.data_type)
                for value in filter_phrase.value_phrases
            ]
        except ValueError as exc:
            problems.append(
                f"{dimension.name!r} holds {dimension.data_type} values, but the "
                f"question compares it to something else: {exc}"
            )
            continue

        query.filters.append(
            QueryFilter(
                dimension_id=dimension.id, operator=filter_phrase.operator, values=values
            )
        )

    if resolved.intent.time_range is not None:
        try:
            time_range = resolve_timeframe(resolved.intent.time_range.phrase, today=today)
        except TimeframeError as exc:
            problems.append(str(exc))
        else:
            dimension = _pick_time_dimension(db, version_id, problems)
            if dimension is not None:
                query.time_dimension_id = dimension.id
                query.time_range = time_range
                query.grain = time_range.grain

                if (
                    dimension.granularity is not None
                    and time_range.grain is not None
                    and _finer(time_range.grain, dimension.granularity)
                ):
                    problems.append(
                        f"{dimension.name!r} is only recorded to the "
                        f"{dimension.granularity}, so it cannot answer a question "
                        f"bucketed by {time_range.grain}."
                    )

    if problems:
        return BuildResult(query=None, problems=problems)
    return BuildResult(query=query, problems=[])


_GRAIN_ORDER = [
    TimeGranularity.DAY,
    TimeGranularity.WEEK,
    TimeGranularity.MONTH,
    TimeGranularity.QUARTER,
    TimeGranularity.YEAR,
]


def _finer(requested: TimeGranularity, available: TimeGranularity) -> bool:
    return _GRAIN_ORDER.index(requested) < _GRAIN_ORDER.index(available)


def describe(db: Session, query: SemanticQuery) -> str:
    """A one-line summary for logs and error messages."""
    metrics = list(db.scalars(select(Metric.name).where(Metric.id.in_(query.metric_ids))))
    dimensions = list(
        db.scalars(select(Dimension.name).where(Dimension.id.in_(query.dimension_ids)))
    )
    parts = [f"metrics={metrics}"]
    if dimensions:
        parts.append(f"by={dimensions}")
    if query.time_range:
        parts.append(f"from={query.time_range.start} to={query.time_range.end}")
    if query.filters:
        parts.append(f"filters={len(query.filters)}")
    return " ".join(parts)
