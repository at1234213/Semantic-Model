import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, Text, Uuid
from sqlalchemy import Enum as SAEnum
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
    from app.models.document_chunk import DocumentChunk
    from app.models.workspace import Workspace


class DocumentKind(enum.StrEnum):
    NOTE = "note"
    UPLOAD = "upload"
    URL = "url"


class Document(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """Unstructured context attached to a workspace.

    Holds the whole source text. Retrieval works over `document_chunks`, which
    are derived from it — `content_hash` is what tells you the chunks are stale.
    """

    __tablename__ = "documents"
    __table_args__ = (
        tenant_unique("documents"),
        parent_fk("documents", "workspace_id", "workspaces"),
        CheckConstraint("length(trim(title)) > 0", name="title_present"),
        CheckConstraint("length(content_hash) = 64", name="content_hash_is_sha256"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)

    kind: Mapped[DocumentKind] = mapped_column(
        SAEnum(
            DocumentKind,
            name="document_kind",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=DocumentKind.NOTE,
        server_default=DocumentKind.NOTE.value,
    )
    source_uri: Mapped[str | None] = mapped_column(String(2048), default=None)

    # sha256 of `content`. Chunks record the hash they were built from, so a
    # mismatch means they need rebuilding — and an unchanged document can skip
    # re-embedding entirely.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    workspace: Mapped["Workspace"] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentChunk.ordinal",
    )

    def __repr__(self) -> str:
        return f"<Document id={self.id} title={self.title!r} kind={self.kind}>"
