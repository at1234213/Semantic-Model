"""Step 21: documents and their chunks."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentKind, Tenant, Workspace
from app.services.chunking import chunk_text, content_hash


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _seed(db: Session, tenant_name: str = "acme") -> Workspace:
    tenant = Tenant(name=tenant_name)
    db.add(tenant)
    db.flush()
    _scope(db, tenant.id)
    workspace = Workspace(tenant_id=tenant.id, name="analytics")
    db.add(workspace)
    db.flush()
    return workspace


def _document(db: Session, workspace: Workspace, content: str = "Some notes.",
              **kwargs) -> Document:
    defaults = {
        "tenant_id": workspace.tenant_id,
        "workspace_id": workspace.id,
        "title": "Q3 notes",
        "content": content,
        "content_hash": content_hash(content),
    }
    document = Document(**{**defaults, **kwargs})
    db.add(document)
    db.flush()
    return document


def _chunks(db: Session, document: Document, max_chars: int = 200) -> list[DocumentChunk]:
    pieces = chunk_text(document.content, max_chars=max_chars)
    chunks = [
        DocumentChunk(
            tenant_id=document.tenant_id,
            document_id=document.id,
            ordinal=index,
            content=piece,
            char_count=len(piece),
            source_hash=document.content_hash,
        )
        for index, piece in enumerate(pieces)
    ]
    db.add_all(chunks)
    db.flush()
    return chunks


def test_document_defaults_to_a_note(db_session: Session) -> None:
    workspace = _seed(db_session)
    document = _document(db_session, workspace)
    db_session.refresh(document)
    assert document.kind == DocumentKind.NOTE
    assert document.source_uri is None


def test_kind_stores_its_value(db_session: Session) -> None:
    workspace = _seed(db_session)
    _document(db_session, workspace, kind=DocumentKind.URL,
              source_uri="https://example.com/handbook")
    assert db_session.execute(text("SELECT kind FROM documents")).scalar_one() == "url"


def test_invalid_kind_is_rejected(db_session: Session) -> None:
    workspace = _seed(db_session)
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO documents (id, tenant_id, workspace_id, title, content, "
                "kind, content_hash, created_at, updated_at) VALUES "
                "(:i, :t, :w, 'x', 'y', 'nonsense', :h, now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(workspace.tenant_id),
             "w": str(workspace.id), "h": "a" * 64},
        )


def test_blank_title_is_rejected(db_session: Session) -> None:
    workspace = _seed(db_session)
    with pytest.raises(IntegrityError):
        _document(db_session, workspace, title="   ")


@pytest.mark.parametrize("bad_hash", ["", "abc", "a" * 63, "a" * 65])
def test_malformed_content_hash_is_rejected(db_session: Session, bad_hash: str) -> None:
    """Two mechanisms reject these: the CHECK catches wrong-but-short values,
    and VARCHAR(64) catches over-length ones before the CHECK is reached."""
    workspace = _seed(db_session)
    with pytest.raises((IntegrityError, DataError)):
        _document(db_session, workspace, content_hash=bad_hash)


def test_chunks_are_ordered(db_session: Session) -> None:
    workspace = _seed(db_session)
    document = _document(db_session, workspace, "para one.\n\n" * 60)
    _chunks(db_session, document)

    db_session.refresh(document)
    assert len(document.chunks) > 1
    assert [c.ordinal for c in document.chunks] == list(range(len(document.chunks)))


def test_duplicate_ordinal_is_rejected(db_session: Session) -> None:
    workspace = _seed(db_session)
    document = _document(db_session, workspace)
    _chunks(db_session, document)

    db_session.add(
        DocumentChunk(
            tenant_id=document.tenant_id, document_id=document.id, ordinal=0,
            content="dup", char_count=3, source_hash=document.content_hash,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_empty_chunk_is_rejected(db_session: Session) -> None:
    workspace = _seed(db_session)
    document = _document(db_session, workspace)
    db_session.add(
        DocumentChunk(
            tenant_id=document.tenant_id, document_id=document.id, ordinal=5,
            content="", char_count=0, source_hash=document.content_hash,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_source_hash_detects_stale_chunks(db_session: Session) -> None:
    """The whole point of carrying the hash: knowing when to re-chunk."""
    workspace = _seed(db_session)
    document = _document(db_session, workspace, "original text")
    _chunks(db_session, document)
    assert all(c.source_hash == document.content_hash for c in document.chunks)

    document.content = "edited text"
    document.content_hash = content_hash("edited text")
    db_session.flush()
    db_session.refresh(document)
    assert all(c.source_hash != document.content_hash for c in document.chunks)


def test_chunk_cannot_claim_a_different_tenant(db_session: Session) -> None:
    workspace = _seed(db_session)
    document = _document(db_session, workspace)
    other = Tenant(name="globex")
    db_session.add(other)
    db_session.flush()
    _scope(db_session, workspace.tenant_id)

    with pytest.raises((IntegrityError, ProgrammingError)):
        db_session.execute(
            text(
                "INSERT INTO document_chunks (id, tenant_id, document_id, ordinal, "
                "content, char_count, source_hash, created_at, updated_at) VALUES "
                "(:i, :t, :d, 0, 'x', 1, :h, now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(other.id), "d": str(document.id),
             "h": document.content_hash},
        )


def test_rls_isolates_documents_and_chunks(db_session: Session) -> None:
    acme = _seed(db_session, "acme")
    globex = _seed(db_session, "globex")
    globex_doc = _document(db_session, globex, "globex content")
    _chunks(db_session, globex_doc)

    _scope(db_session, acme.tenant_id)
    acme_doc = _document(db_session, acme, "acme content")
    _chunks(db_session, acme_doc)

    assert db_session.execute(text("SELECT count(*) FROM documents")).scalar_one() == 1
    assert db_session.execute(
        text("SELECT DISTINCT content FROM document_chunks")
    ).scalars().all() == ["acme content"]


def test_deleting_a_document_cascades_to_chunks(db_session: Session) -> None:
    workspace = _seed(db_session)
    document = _document(db_session, workspace, "para.\n\n" * 40)
    _chunks(db_session, document)

    db_session.delete(document)
    db_session.flush()
    assert db_session.execute(text("SELECT count(*) FROM document_chunks")).scalar_one() == 0
