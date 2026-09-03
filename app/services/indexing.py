"""Keeping chunk embeddings in step with their text.

A chunk needs (re)embedding when it has no vector, or when the vector was made
by a different encoder than the one now configured.
"""

import uuid

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.services.chunking import chunk_text, content_hash
from app.services.embeddings import Embedder, get_embedder

# Encoders are far faster per item in batches than one at a time.
DEFAULT_BATCH = 64


def rebuild_chunks(db: Session, document: Document, **chunk_options) -> list[DocumentChunk]:
    """Replace a document's chunks from its current content.

    Existing chunks are deleted rather than reconciled: chunking is
    deterministic, so anything built from a different content_hash is wrong
    rather than merely out of date.
    """
    document.content_hash = content_hash(document.content)
    # Delete by query, not through document.chunks: that collection does not
    # contain rows a previous call added straight to the session, so deleting
    # through it leaves the old chunks in place and the next insert collides on
    # (document_id, ordinal). Same trap as metric references in Step 18.
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
    db.expire(document, ["chunks"])

    chunks = [
        DocumentChunk(
            tenant_id=document.tenant_id,
            document_id=document.id,
            ordinal=index,
            content=piece,
            char_count=len(piece),
            source_hash=document.content_hash,
        )
        for index, piece in enumerate(chunk_text(document.content, **chunk_options))
    ]
    db.add_all(chunks)
    db.flush()
    db.refresh(document, ["chunks"])
    return chunks


def pending_chunks(
    db: Session, *, embedder: Embedder | None = None, limit: int | None = None
) -> list[DocumentChunk]:
    embedder = embedder or get_embedder()
    statement = select(DocumentChunk).where(
        or_(
            DocumentChunk.embedding.is_(None),
            DocumentChunk.embedding_model != embedder.name,
        )
    ).order_by(DocumentChunk.document_id, DocumentChunk.ordinal)
    if limit is not None:
        statement = statement.limit(limit)
    return list(db.scalars(statement))


def embed_chunks(
    db: Session,
    chunks: list[DocumentChunk],
    *,
    embedder: Embedder | None = None,
    batch_size: int = DEFAULT_BATCH,
) -> int:
    embedder = embedder or get_embedder()
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        for chunk, vector in zip(batch, embedder.embed([c.content for c in batch]), strict=True):
            chunk.embedding = vector
            chunk.embedding_model = embedder.name
        db.flush()
    return len(chunks)


def embed_pending(
    db: Session, *, embedder: Embedder | None = None, limit: int | None = None
) -> int:
    embedder = embedder or get_embedder()
    return embed_chunks(db, pending_chunks(db, embedder=embedder, limit=limit), embedder=embedder)


def search_chunks(
    db: Session,
    query: str,
    *,
    workspace_id: uuid.UUID | None = None,
    limit: int = 5,
    embedder: Embedder | None = None,
) -> list[tuple[DocumentChunk, float]]:
    """Nearest chunks by cosine distance, closest first.

    Only compares against vectors from the same encoder — a distance across two
    models is not a smaller number, it is a meaningless one.
    """
    embedder = embedder or get_embedder()
    vector = embedder.embed([query])[0]

    distance = DocumentChunk.embedding.cosine_distance(vector)
    statement = (
        select(DocumentChunk, distance.label("distance"))
        .where(
            DocumentChunk.embedding.is_not(None),
            DocumentChunk.embedding_model == embedder.name,
        )
        .order_by(distance)
        .limit(limit)
    )
    if workspace_id is not None:
        statement = statement.join(
            Document, Document.id == DocumentChunk.document_id
        ).where(Document.workspace_id == workspace_id)

    return [(chunk, float(dist)) for chunk, dist in db.execute(statement)]
