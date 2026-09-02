"""Resolving metric expressions into dependency edges.

Names are resolved and cycles detected once, when a metric is written. Query
time then walks `metric_references` as a graph instead of parsing strings.
"""

import uuid
from collections import deque

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.measure import Measure
from app.models.metric import Metric
from app.models.metric_reference import MetricReference
from app.services.expressions import ExpressionError, validate_metric_expression


class SemanticNameError(ValueError):
    """A referenced name is missing, ambiguous, or already taken."""


class CircularReferenceError(ValueError):
    """A metric would end up depending on itself."""


def assert_name_available(
    db: Session, version_id: uuid.UUID, name: str, *, exclude_metric_id: uuid.UUID | None = None
) -> None:
    """Measures and metrics share one namespace.

    Nothing in the schema can express that across two tables, and allowing a
    measure and a metric to both be called `revenue` would leave the retrieval
    agent with no way to know which one was meant.
    """
    measure_exists = db.execute(
        select(Measure.id).where(
            Measure.semantic_model_version_id == version_id, Measure.name == name
        )
    ).first()
    if measure_exists:
        raise SemanticNameError(f"A measure named {name!r} already exists in this version")

    metric_query = select(Metric.id).where(
        Metric.semantic_model_version_id == version_id, Metric.name == name
    )
    if exclude_metric_id is not None:
        metric_query = metric_query.where(Metric.id != exclude_metric_id)
    if db.execute(metric_query).first():
        raise SemanticNameError(f"A metric named {name!r} already exists in this version")


def _resolve(db: Session, version_id: uuid.UUID, name: str) -> tuple[str, uuid.UUID]:
    measure_id = db.execute(
        select(Measure.id).where(
            Measure.semantic_model_version_id == version_id, Measure.name == name
        )
    ).scalar_one_or_none()
    metric_id = db.execute(
        select(Metric.id).where(
            Metric.semantic_model_version_id == version_id, Metric.name == name
        )
    ).scalar_one_or_none()

    if measure_id and metric_id:
        raise SemanticNameError(
            f"{name!r} is both a measure and a metric in this version, so a reference "
            "to it is ambiguous"
        )
    if measure_id:
        return "measure", measure_id
    if metric_id:
        return "metric", metric_id
    raise SemanticNameError(f"{name!r} is not a measure or metric in this version")


def _dependencies(db: Session, metric_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    if not metric_ids:
        return []
    return list(
        db.execute(
            select(MetricReference.ref_metric_id).where(
                MetricReference.metric_id.in_(metric_ids),
                MetricReference.ref_metric_id.is_not(None),
            )
        ).scalars()
    )


def assert_no_cycle(db: Session, metric_id: uuid.UUID, ref_metric_ids: list[uuid.UUID]) -> None:
    """Walk forward from the proposed references; reaching `metric_id` is a cycle."""
    if metric_id in ref_metric_ids:
        raise CircularReferenceError("A metric cannot reference itself")

    seen: set[uuid.UUID] = set()
    frontier = deque(ref_metric_ids)
    while frontier:
        current = frontier.popleft()
        if current in seen:
            continue
        seen.add(current)
        for dependency in _dependencies(db, [current]):
            if dependency == metric_id:
                raise CircularReferenceError(
                    "That expression would make this metric depend on itself"
                )
            frontier.append(dependency)


def set_expression(db: Session, metric: Metric, expression: str) -> list[MetricReference]:
    """Validate an expression and replace the metric's dependency edges.

    Raises ExpressionError, SemanticNameError or CircularReferenceError; the
    caller owns the transaction, so a failure leaves nothing half-written.
    """
    names = validate_metric_expression(expression)
    version_id = metric.semantic_model_version_id

    resolved = [_resolve(db, version_id, name) for name in names]
    ref_metric_ids = [target for kind, target in resolved if kind == "metric"]
    assert_no_cycle(db, metric.id, ref_metric_ids)

    # Delete by query rather than through metric.references: that collection
    # does not contain rows a previous call added directly to the session, so
    # deleting through it silently left old edges behind.
    db.execute(delete(MetricReference).where(MetricReference.metric_id == metric.id))
    db.expire(metric, ["references"])

    references = [
        MetricReference(
            tenant_id=metric.tenant_id,
            metric_id=metric.id,
            ref_measure_id=target if kind == "measure" else None,
            ref_metric_id=target if kind == "metric" else None,
        )
        for kind, target in resolved
    ]
    db.add_all(references)
    metric.expression = expression
    db.flush()
    db.refresh(metric, ["references"])
    return references


__all__ = [
    "CircularReferenceError",
    "ExpressionError",
    "SemanticNameError",
    "assert_name_available",
    "assert_no_cycle",
    "set_expression",
]
