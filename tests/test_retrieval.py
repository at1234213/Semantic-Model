"""Step 23: hybrid retrieval over the semantic layer."""

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
from app.services.embeddings import HashEmbedder
from app.services.retrieval import rebuild_search_index, retrieve


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _model(db: Session, tenant_name: str = "acme") -> SemanticModelVersion:
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
    return version


def _populate(db: Session, version: SemanticModelVersion) -> dict[str, uuid.UUID]:
    """A small but realistic model: one entity, dimensions, measures, metrics."""
    tenant_id = version.tenant_id
    ids: dict[str, uuid.UUID] = {}

    order = Entity(
        tenant_id=tenant_id, semantic_model_version_id=version.id,
        name="order", display_name="Order", source_table="orders",
        description="A purchase placed by a customer",
    )
    customer = Entity(
        tenant_id=tenant_id, semantic_model_version_id=version.id,
        name="customer", display_name="Customer", source_table="customers",
        description="A person or company that buys from us",
    )
    db.add_all([order, customer])
    db.flush()
    ids["order"], ids["customer"] = order.id, customer.id

    country = Dimension(
        tenant_id=tenant_id, entity_id=customer.id, name="country",
        expression="country", description="The country a customer is billed in",
    )
    db.add(country)
    db.flush()
    ids["country"] = country.id

    gross = Measure(
        tenant_id=tenant_id, entity_id=order.id, semantic_model_version_id=version.id,
        name="gross_revenue", aggregation=Aggregation.SUM, expression="amount",
    )
    db.add(gross)
    db.flush()

    revenue = Metric(
        tenant_id=tenant_id, semantic_model_version_id=version.id, name="revenue",
        display_name="Revenue", expression="${gross_revenue}",
        description="Total money billed before refunds",
    )
    shipping = Metric(
        tenant_id=tenant_id, semantic_model_version_id=version.id, name="shipping_cost",
        expression="${gross_revenue}", description="What we pay carriers to deliver orders",
    )
    db.add_all([revenue, shipping])
    db.flush()
    ids["revenue"], ids["shipping_cost"] = revenue.id, shipping.id

    db.add(
        Synonym(
            tenant_id=tenant_id, semantic_model_version_id=version.id,
            term="turnover", metric_id=revenue.id,
        )
    )
    db.add(
        GlossaryTerm(
            tenant_id=tenant_id, semantic_model_version_id=version.id,
            term="active customer",
            definition="A customer with at least one order in the last 90 days",
        )
    )
    db.flush()
    return ids


# ---------- building the index ----------


def test_index_covers_every_searchable_object(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)

    assert rebuild_search_index(db_session, version.id) == 6  # 2 entities, 1 dim, 2 metrics, 1 term

    types = db_session.execute(
        text("SELECT object_type, count(*) FROM semantic_search_index GROUP BY 1 ORDER BY 1")
    ).all()
    assert dict(types) == {"dimension": 1, "entity": 2, "glossary_term": 1, "metric": 2}


def test_rebuilding_is_idempotent(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    first = rebuild_search_index(db_session, version.id)
    second = rebuild_search_index(db_session, version.id)

    assert first == second
    assert db_session.execute(
        text("SELECT count(*) FROM semantic_search_index")
    ).scalar_one() == second


def test_synonyms_are_folded_into_the_targets_text(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    row = db_session.execute(
        text("SELECT search_text FROM semantic_search_index WHERE name = 'revenue'")
    ).scalar_one()
    assert "turnover" in row


def test_search_vector_is_maintained_by_postgres(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    populated = db_session.execute(
        text("SELECT count(*) FROM semantic_search_index WHERE search_vector IS NOT NULL")
    ).scalar_one()
    assert populated == 6


# ---------- retrieval ----------


def test_exact_name_wins(db_session: Session) -> None:
    version = _model(db_session)
    ids = _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    hits = retrieve(db_session, "revenue", version_id=version.id)
    assert hits[0].object_id == ids["revenue"]
    assert "exact" in hits[0].matched_by


def test_a_synonym_finds_its_metric(db_session: Session) -> None:
    """'turnover' appears nowhere in the metric's name."""
    version = _model(db_session)
    ids = _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    hits = retrieve(db_session, "turnover", version_id=version.id)
    assert hits[0].object_id == ids["revenue"]


def test_a_description_phrase_finds_the_object(db_session: Session) -> None:
    version = _model(db_session)
    ids = _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    hits = retrieve(db_session, "what we pay carriers", version_id=version.id)
    assert ids["shipping_cost"] in [hit.object_id for hit in hits]


def test_results_carry_which_methods_matched(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    hits = retrieve(db_session, "revenue", version_id=version.id)
    assert hits[0].matched_by
    assert set(hits[0].matched_by) <= {"exact", "lexical", "vector"}


def test_scores_are_descending(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    scores = [hit.score for hit in retrieve(db_session, "customer country", version_id=version.id)]
    assert scores == sorted(scores, reverse=True)


def test_object_types_are_returned(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    hits = retrieve(db_session, "active customer", version_id=version.id)
    assert SearchObjectType.GLOSSARY_TERM in [hit.object_type for hit in hits]


def test_nonsense_query_returns_few_or_no_hits(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    hits = retrieve(db_session, "zzzz qqqq wombat", version_id=version.id, limit=10)
    assert all("exact" not in hit.matched_by for hit in hits)


def test_retrieval_is_scoped_to_one_version(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    other = SemanticModelVersion(
        tenant_id=version.tenant_id, semantic_model_id=version.semantic_model_id,
        version=2, status=VersionStatus.DRAFT,
    )
    db_session.add(other)
    db_session.flush()

    assert retrieve(db_session, "revenue", version_id=other.id) == []


def test_retrieval_cannot_cross_a_tenant_boundary(db_session: Session) -> None:
    acme = _model(db_session, "acme")
    acme_ids = _populate(db_session, acme)
    rebuild_search_index(db_session, acme.id)

    globex = _model(db_session, "globex")  # scope switches to globex
    _populate(db_session, globex)
    rebuild_search_index(db_session, globex.id)

    # Still scoped to globex: acme's version id must yield nothing.
    assert retrieve(db_session, "revenue", version_id=acme.id) == []
    assert acme_ids["revenue"] not in [
        hit.object_id for hit in retrieve(db_session, "revenue", version_id=globex.id)
    ]


def test_a_different_encoder_disables_the_vector_leg(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    class OtherEmbedder(HashEmbedder):
        name = "other-v1"

    hits = retrieve(db_session, "revenue", version_id=version.id, embedder=OtherEmbedder())
    assert all("vector" not in hit.matched_by for hit in hits)
    assert hits  # exact and lexical still work


def test_deleting_a_version_clears_its_index(db_session: Session) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)

    db_session.delete(version)
    db_session.flush()
    assert db_session.execute(
        text("SELECT count(*) FROM semantic_search_index")
    ).scalar_one() == 0


@pytest.mark.parametrize("query", ["", "   "])
def test_blank_query_does_not_error(db_session: Session, query: str) -> None:
    version = _model(db_session)
    _populate(db_session, version)
    rebuild_search_index(db_session, version.id)
    retrieve(db_session, query, version_id=version.id)
