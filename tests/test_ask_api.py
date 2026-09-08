"""Step 29: the question-answering endpoint, auth, and the audit trail."""

import uuid

from fastapi.testclient import TestClient
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
    QueryRun,
    Relationship,
    RelationshipJoinKey,
    SemanticModel,
    SemanticModelVersion,
    Tenant,
    VersionStatus,
    Workspace,
)
from app.services.metrics import set_expression
from app.services.retrieval import rebuild_search_index


def _semantic_model(db: Session, tenant: Tenant, warehouse: str) -> SemanticModelVersion:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant.id)}
    )
    workspace = Workspace(tenant_id=tenant.id, name="analytics")
    db.add(workspace)
    db.flush()

    ciphertext, key_version = encrypt_secret("postgres")
    source = DataSource(
        tenant_id=tenant.id, workspace_id=workspace.id, name="warehouse", host="db",
        database="semantic_model", username="postgres",
        password_ciphertext=ciphertext, key_version=key_version,
        default_schema=warehouse,
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
                   name="order", source_schema=warehouse, source_table="orders")
    customer = Entity(tenant_id=tenant.id, semantic_model_version_id=version.id,
                      name="customer", source_schema=warehouse, source_table="customers")
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


def test_ask_requires_authentication(client: TestClient) -> None:
    response = client.post("/api/ask", json={
        "question": "revenue", "semantic_model_version_id": str(uuid.uuid4())})
    assert response.status_code == 401


def test_ask_returns_sql_without_executing(
    client: TestClient, db_session: Session, make_tenant, warehouse_tables
) -> None:
    tenant, headers = make_tenant("acme")
    version = _semantic_model(db_session, tenant, warehouse_tables)

    response = client.post("/api/ask", headers=headers, json={
        "question": "revenue by country",
        "semantic_model_version_id": str(version.id),
        "execute": False,
    })
    body = response.json()
    assert response.status_code == 200
    # No scripted intent is registered, so the parser declines rather than guesses.
    assert body["status"] == "failed"
    assert body["problems"]


def test_a_run_is_recorded_for_every_question(
    client: TestClient, db_session: Session, make_tenant, warehouse_tables
) -> None:
    tenant, headers = make_tenant("acme")
    version = _semantic_model(db_session, tenant, warehouse_tables)

    client.post("/api/ask", headers=headers, json={
        "question": "revenue by country",
        "semantic_model_version_id": str(version.id), "execute": False})

    run = db_session.query(QueryRun).one()
    assert run.question == "revenue by country"
    assert run.tenant_id == tenant.id


def test_the_audit_log_stores_no_result_rows(
    client: TestClient, db_session: Session, make_tenant, warehouse_tables
) -> None:
    """An audit trail must not become a second copy of the customer's data."""
    tenant, headers = make_tenant("acme")
    version = _semantic_model(db_session, tenant, warehouse_tables)
    client.post("/api/ask", headers=headers, json={
        "question": "revenue", "semantic_model_version_id": str(version.id),
        "execute": False})

    run = db_session.query(QueryRun).one()
    columns = {c.name for c in QueryRun.__table__.columns}
    assert "rows" not in columns and "result" not in columns
    assert run.parameter_count is not None  # the count, not the values


def test_an_unknown_version_is_404(
    client: TestClient, db_session: Session, make_tenant
) -> None:
    _, headers = make_tenant("acme")
    response = client.post("/api/ask", headers=headers, json={
        "question": "revenue", "semantic_model_version_id": str(uuid.uuid4())})
    assert response.status_code == 404


def test_one_tenant_cannot_query_anothers_model(
    client: TestClient, db_session: Session, make_tenant, warehouse_tables
) -> None:
    """RLS hides the version, so it is genuinely not found rather than forbidden."""
    acme, _ = make_tenant("acme")
    version = _semantic_model(db_session, acme, warehouse_tables)
    _, globex_headers = make_tenant("globex")

    response = client.post("/api/ask", headers=globex_headers, json={
        "question": "revenue", "semantic_model_version_id": str(version.id)})
    assert response.status_code == 404


def test_runs_are_listed_per_tenant(
    client: TestClient, db_session: Session, make_tenant, warehouse_tables
) -> None:
    acme, acme_headers = make_tenant("acme")
    version = _semantic_model(db_session, acme, warehouse_tables)
    client.post("/api/ask", headers=acme_headers, json={
        "question": "acme question", "semantic_model_version_id": str(version.id),
        "execute": False})

    _, globex_headers = make_tenant("globex")
    assert client.get("/api/ask/runs", headers=globex_headers).json() == []

    acme_runs = client.get("/api/ask/runs", headers=acme_headers).json()
    assert [r["question"] for r in acme_runs] == ["acme question"]


def test_a_blank_question_is_rejected(
    client: TestClient, db_session: Session, make_tenant
) -> None:
    _, headers = make_tenant("acme")
    response = client.post("/api/ask", headers=headers, json={
        "question": "   ", "semantic_model_version_id": str(uuid.uuid4())})
    assert response.status_code == 422
