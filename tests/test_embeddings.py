"""Step 22: embeddings, the vector column, and similarity search."""

import math
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Document, Tenant, Workspace
from app.services.chunking import content_hash
from app.services.embeddings import (
    EMBEDDING_DIMENSIONS,
    Embedder,
    HashEmbedder,
    get_embedder,
)
from app.services.indexing import (
    embed_pending,
    pending_chunks,
    rebuild_chunks,
    search_chunks,
)


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _workspace(db: Session, tenant_name: str = "acme") -> Workspace:
    tenant = Tenant(name=tenant_name)
    db.add(tenant)
    db.flush()
    _scope(db, tenant.id)
    workspace = Workspace(tenant_id=tenant.id, name="analytics")
    db.add(workspace)
    db.flush()
    return workspace


def _document(db: Session, workspace: Workspace, content: str, title: str = "doc") -> Document:
    document = Document(
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        title=title,
        content=content,
        content_hash=content_hash(content),
    )
    db.add(document)
    db.flush()
    return document


# ---------- the embedder ----------


def test_hash_embedder_is_deterministic() -> None:
    embedder = HashEmbedder()
    assert embedder.embed(["revenue by region"]) == embedder.embed(["revenue by region"])


def test_vectors_are_unit_length() -> None:
    [vector] = HashEmbedder().embed(["some ordinary text"])
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


def test_empty_text_gives_a_zero_vector() -> None:
    """Honest answer for text with no direction; cosine treats it as maximally distant."""
    [vector] = HashEmbedder().embed(["   ...   "])
    assert all(value == 0.0 for value in vector)


def test_lexical_overlap_scores_higher_than_none() -> None:
    a, b, c = HashEmbedder().embed(
        ["quarterly revenue by region", "revenue by region quarterly", "wombat husbandry"]
    )
    dot = lambda x, y: sum(i * j for i, j in zip(x, y, strict=True))  # noqa: E731
    assert dot(a, b) > dot(a, c)


def test_dimensions_match_the_column() -> None:
    assert len(HashEmbedder().embed(["x"])[0]) == EMBEDDING_DIMENSIONS


def test_factory_returns_the_configured_provider() -> None:
    embedder = get_embedder()
    assert isinstance(embedder, Embedder)
    assert embedder.name == "hash-v1"


# ---------- the column ----------


def test_vector_round_trips_through_postgres(db_session: Session) -> None:
    workspace = _workspace(db_session)
    document = _document(db_session, workspace, "hello world")
    rebuild_chunks(db_session, document)
    embed_pending(db_session)

    db_session.refresh(document)
    chunk = document.chunks[0]
    assert chunk.embedding is not None
    assert len(chunk.embedding) == EMBEDDING_DIMENSIONS
    assert chunk.embedding_model == "hash-v1"


def test_embedding_without_a_model_name_is_rejected(db_session: Session) -> None:
    """A vector whose encoder is unknown cannot be compared to anything."""
    workspace = _workspace(db_session)
    document = _document(db_session, workspace, "hello")
    [chunk] = rebuild_chunks(db_session, document)

    chunk.embedding = [0.0] * EMBEDDING_DIMENSIONS
    chunk.embedding_model = None
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------- keeping embeddings in step ----------


def test_new_chunks_are_pending(db_session: Session) -> None:
    workspace = _workspace(db_session)
    document = _document(db_session, workspace, "para one.\n\n" * 30)
    chunks = rebuild_chunks(db_session, document)
    assert len(pending_chunks(db_session)) == len(chunks)


def test_embedding_clears_the_pending_queue(db_session: Session) -> None:
    workspace = _workspace(db_session)
    document = _document(db_session, workspace, "para one.\n\n" * 30)
    rebuild_chunks(db_session, document)

    assert embed_pending(db_session) > 0
    assert pending_chunks(db_session) == []


def test_a_different_encoder_makes_everything_pending_again(db_session: Session) -> None:
    """Vectors from two models are not comparable, so switching invalidates them all."""
    workspace = _workspace(db_session)
    document = _document(db_session, workspace, "hello world")
    rebuild_chunks(db_session, document)
    embed_pending(db_session)
    assert pending_chunks(db_session) == []

    class OtherEmbedder(HashEmbedder):
        name = "other-v1"

    assert len(pending_chunks(db_session, embedder=OtherEmbedder())) == 1


def test_editing_a_document_rebuilds_and_requeues(db_session: Session) -> None:
    workspace = _workspace(db_session)
    document = _document(db_session, workspace, "original text")
    rebuild_chunks(db_session, document)
    embed_pending(db_session)

    document.content = "completely different text"
    rebuild_chunks(db_session, document)
    assert len(pending_chunks(db_session)) == 1


# ---------- search ----------


def _corpus(db: Session, workspace: Workspace) -> None:
    for title, body in [
        ("finance", "quarterly revenue and gross margin by region"),
        ("ops", "warehouse shipping logistics and delivery times"),
        ("hr", "employee onboarding and holiday policy"),
    ]:
        rebuild_chunks(db, _document(db, workspace, body, title=title))
    embed_pending(db)


def test_search_returns_the_closest_chunk_first(db_session: Session) -> None:
    workspace = _workspace(db_session)
    _corpus(db_session, workspace)

    results = search_chunks(db_session, "revenue by region", limit=3)
    assert results
    assert "revenue" in results[0][0].content


def test_search_returns_distances_in_ascending_order(db_session: Session) -> None:
    workspace = _workspace(db_session)
    _corpus(db_session, workspace)

    distances = [distance for _, distance in search_chunks(db_session, "shipping", limit=3)]
    assert distances == sorted(distances)


def test_search_ignores_vectors_from_another_encoder(db_session: Session) -> None:
    workspace = _workspace(db_session)
    _corpus(db_session, workspace)

    class OtherEmbedder(HashEmbedder):
        name = "other-v1"

    assert search_chunks(db_session, "revenue", embedder=OtherEmbedder()) == []


def test_search_can_be_scoped_to_one_workspace(db_session: Session) -> None:
    workspace = _workspace(db_session)
    _corpus(db_session, workspace)

    other = Workspace(tenant_id=workspace.tenant_id, name="second")
    db_session.add(other)
    db_session.flush()
    rebuild_chunks(db_session, _document(db_session, other, "revenue in the other workspace"))
    embed_pending(db_session)

    results = search_chunks(db_session, "revenue", workspace_id=other.id, limit=5)
    assert len(results) == 1
    assert "other workspace" in results[0][0].content


def test_search_cannot_cross_a_tenant_boundary(db_session: Session) -> None:
    """Row-level security applies to vector search like any other query."""
    acme = _workspace(db_session, "acme")
    _corpus(db_session, acme)

    globex = _workspace(db_session, "globex")  # scope switches to globex
    rebuild_chunks(db_session, _document(db_session, globex, "globex secret revenue figures"))
    embed_pending(db_session)

    results = search_chunks(db_session, "revenue", limit=10)
    assert results
    assert all("globex" in chunk.content for chunk, _ in results)
