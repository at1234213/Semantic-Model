import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    parent_fk,
    tenant_unique,
)

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
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)

    # The document content_hash this chunk was built from.
    source_hash: Mapped[str] = mapped_column(String(64))

    document: Mapped["Document"] = relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return f"<DocumentChunk doc={self.document_id} #{self.ordinal} {self.char_count}ch>"
