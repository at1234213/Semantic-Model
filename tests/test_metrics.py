"""Step 18: measures, metrics, and dependency resolution."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Aggregation,
    Entity,
    Measure,
    Metric,
    SemanticModel,
    SemanticModelVersion,
    Tenant,
    VersionStatus,
    Workspace,
)
from app.services import metrics as metrics_service
from app.services.expressions import ExpressionError
from app.services.metrics import CircularReferenceError, SemanticNameError


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
        name="order", source_table="orders",
    )
    db.add(entity)
    db.flush()
    return version, entity


def _measure(db: Session, entity: Entity, version: SemanticModelVersion, name: str,
             expression: str = "amount",
             aggregation: Aggregation = Aggregation.SUM) -> Measure:
    measure = Measure(
        tenant_id=entity.tenant_id,
        entity_id=entity.id,
        semantic_model_version_id=version.id,
        name=name,
        aggregation=aggregation,
        expression=expression,
    )
    db.add(measure)
    db.flush()
    return measure


def _metric(db: Session, version: SemanticModelVersion, name: str,
            expression: str = "${gross_revenue}") -> Metric:
    metric = Metric(
        tenant_id=version.tenant_id,
        semantic_model_version_id=version.id,
        name=name,
        expression=expression,
    )
    db.add(metric)
    db.flush()
    return metric


# ---------- measures ----------


def test_measure_stores_aggregation_as_its_value(db_session: Session) -> None:
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "order_count", "id", Aggregation.COUNT_DISTINCT)
    assert db_session.execute(text("SELECT aggregation FROM measures")).scalar_one() == (
        "count_distinct"
    )


def test_measure_names_are_unique_across_the_whole_version(db_session: Session) -> None:
    """Not merely per entity: `${gross_revenue}` must be unambiguous."""
    version, entity = _seed(db_session)
    other = Entity(
        tenant_id=entity.tenant_id, semantic_model_version_id=version.id,
        name="customer", source_table="customers",
    )
    db_session.add(other)
    db_session.flush()

    _measure(db_session, entity, version, "total")
    with pytest.raises(IntegrityError):
        _measure(db_session, other, version, "total")


def test_measure_cannot_reference_an_entity_from_another_version(db_session: Session) -> None:
    """The three-column foreign key pins entity, version and tenant together."""
    version, entity = _seed(db_session)
    other_version = SemanticModelVersion(
        tenant_id=version.tenant_id, semantic_model_id=version.semantic_model_id,
        version=2, status=VersionStatus.DRAFT,
    )
    db_session.add(other_version)
    db_session.flush()

    with pytest.raises(IntegrityError):
        _measure(db_session, entity, other_version, "mismatched")


@pytest.mark.parametrize("expression", ["amount; DROP TABLE x", "amount -- c", "a'b"])
def test_database_rejects_hostile_measure_expressions(
    db_session: Session, expression: str
) -> None:
    version, entity = _seed(db_session)
    with pytest.raises(IntegrityError):
        _measure(db_session, entity, version, "bad", expression)


# ---------- metric references ----------


def test_set_expression_records_measure_edges(db_session: Session) -> None:
    version, entity = _seed(db_session)
    revenue = _measure(db_session, entity, version, "gross_revenue")
    count = _measure(db_session, entity, version, "order_count", "id",
                     Aggregation.COUNT_DISTINCT)

    metric = _metric(db_session, version, "average_order_value")
    references = metrics_service.set_expression(
        db_session, metric, "${gross_revenue} / ${order_count}"
    )

    assert {r.ref_measure_id for r in references} == {revenue.id, count.id}
    assert all(r.ref_metric_id is None for r in references)


def test_set_expression_records_metric_edges(db_session: Session) -> None:
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "gross_revenue")
    base = _metric(db_session, version, "revenue")
    metrics_service.set_expression(db_session, base, "${gross_revenue}")

    derived = _metric(db_session, version, "revenue_doubled")
    references = metrics_service.set_expression(db_session, derived, "${revenue} * 2")
    assert [r.ref_metric_id for r in references] == [base.id]


def test_unknown_name_is_rejected(db_session: Session) -> None:
    version, _ = _seed(db_session)
    metric = _metric(db_session, version, "broken")
    with pytest.raises(SemanticNameError):
        metrics_service.set_expression(db_session, metric, "${nope}")


def test_bad_grammar_is_rejected_before_any_lookup(db_session: Session) -> None:
    version, _ = _seed(db_session)
    metric = _metric(db_session, version, "broken")
    with pytest.raises(ExpressionError):
        metrics_service.set_expression(db_session, metric, "gross_revenue / order_count")


def test_replacing_an_expression_replaces_its_edges(db_session: Session) -> None:
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "gross_revenue")
    _measure(db_session, entity, version, "order_count", "id", Aggregation.COUNT_DISTINCT)

    metric = _metric(db_session, version, "m")
    metrics_service.set_expression(db_session, metric, "${gross_revenue} / ${order_count}")
    metrics_service.set_expression(db_session, metric, "${gross_revenue}")

    assert db_session.execute(
        text("SELECT count(*) FROM metric_references")
    ).scalar_one() == 1


# ---------- cycles ----------


def test_self_reference_is_rejected(db_session: Session) -> None:
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "gross_revenue")
    metric = _metric(db_session, version, "loop")
    with pytest.raises(CircularReferenceError):
        metrics_service.set_expression(db_session, metric, "${loop} + 1")


def test_two_step_cycle_is_rejected(db_session: Session) -> None:
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "gross_revenue")

    a = _metric(db_session, version, "a")
    metrics_service.set_expression(db_session, a, "${gross_revenue}")
    b = _metric(db_session, version, "b")
    metrics_service.set_expression(db_session, b, "${a}")

    with pytest.raises(CircularReferenceError):
        metrics_service.set_expression(db_session, a, "${b}")


def test_three_step_cycle_is_rejected(db_session: Session) -> None:
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "gross_revenue")

    a = _metric(db_session, version, "a")
    metrics_service.set_expression(db_session, a, "${gross_revenue}")
    b = _metric(db_session, version, "b")
    metrics_service.set_expression(db_session, b, "${a}")
    c = _metric(db_session, version, "c")
    metrics_service.set_expression(db_session, c, "${b}")

    with pytest.raises(CircularReferenceError):
        metrics_service.set_expression(db_session, a, "${c}")


def test_a_diamond_is_not_a_cycle(db_session: Session) -> None:
    """Two metrics depending on the same base is legitimate."""
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "gross_revenue")

    base = _metric(db_session, version, "base")
    metrics_service.set_expression(db_session, base, "${gross_revenue}")
    left = _metric(db_session, version, "left")
    metrics_service.set_expression(db_session, left, "${base} * 2")
    right = _metric(db_session, version, "right")
    metrics_service.set_expression(db_session, right, "${base} / 2")

    top = _metric(db_session, version, "top")
    metrics_service.set_expression(db_session, top, "${left} + ${right}")
    assert len(top.references) == 2


# ---------- shared namespace ----------


def test_a_metric_cannot_take_a_measures_name(db_session: Session) -> None:
    """Their warning: nobody would know which `revenue` the agent meant."""
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "revenue")
    with pytest.raises(SemanticNameError):
        metrics_service.assert_name_available(db_session, version.id, "revenue")


def test_an_unused_name_is_available(db_session: Session) -> None:
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "revenue")
    metrics_service.assert_name_available(db_session, version.id, "margin")


# ---------- isolation ----------


def test_rls_isolates_metrics(db_session: Session) -> None:
    acme_version, acme_entity = _seed(db_session, "acme")
    globex_version, _ = _seed(db_session, "globex")
    _metric(db_session, globex_version, "globex_metric", "${x}")

    _scope(db_session, acme_version.tenant_id)
    _measure(db_session, acme_entity, acme_version, "gross_revenue")
    _metric(db_session, acme_version, "acme_metric")

    names = db_session.execute(text("SELECT name FROM metrics")).scalars().all()
    assert names == ["acme_metric"]


def test_deleting_a_metric_cascades_to_its_references(db_session: Session) -> None:
    version, entity = _seed(db_session)
    _measure(db_session, entity, version, "gross_revenue")
    metric = _metric(db_session, version, "m")
    metrics_service.set_expression(db_session, metric, "${gross_revenue}")

    db_session.delete(metric)
    db_session.flush()
    assert db_session.execute(
        text("SELECT count(*) FROM metric_references")
    ).scalar_one() == 0
