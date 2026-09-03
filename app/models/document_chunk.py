import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    parent_fk,
    tenant_unique,
)
from app.services.embeddings import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from app.models.document import Document


class DocumentChunk(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """One retrievable span of a document.

    The unit embeddings attach to in Step 22 and the unit hybrid search returns
    in Step 23. Chunks are derived data: rebuilt whenever the parent document's
    `content_hash` changes.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        tenant_unique("document_chunks"),
        parent_fk("document_chunks", "document_id", "documents"),
        UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_ordinal"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint("char_count > 0", name="char_count_positive"),
        CheckConstraint("length(source_hash) = 64", name="source_hash_is_sha256"),
        # A vector without the name of the model that produced it is unusable:
        # cosine distance between two encoders' output is meaningless, not
        # merely imprecise. They are set and cleared together.
        CheckConstraint(
            "(embedding IS NULL) = (embedding_model IS NULL)",
            name="embedding_has_model",
        ),
        # HNSW over cosine distance. Built on an empty table here; on a large
        # one this is the slow part of the migration.
        Index("ix_document_chunks_fts", "search_vector", postgresql_using="gin"),
        Index(
            "ix_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)

    # The document content_hash this chunk was built from.
    source_hash: Mapped[str] = mapped_column(String(64))

    # Maintained by Postgres from `content`, so it can never drift out of step.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
    )

    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), default=None
    )
    # Which encoder produced it. Also the staleness check when the model changes.
    embedding_model: Mapped[str | None] = mapped_column(String(255), default=None)

    document: Mapped["Document"] = relationship(back_populates="chunks")

    def __repr__(self) -> str:
        embedded = "embedded" if self.embedding is not None else "pending"
        return (
            f"<DocumentChunk doc={self.document_id} #{self.ordinal} "
            f"{self.char_count}ch {embedded}>"
        )
