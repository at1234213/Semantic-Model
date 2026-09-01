import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKeyConstraint, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Unstructured context attached to a workspace.

    Chunking and the pgvector embedding column arrive in Step 21/22; this is
    the raw source record they will hang off.
    """

    __tablename__ = "documents"
    __table_args__ = (
        # Composite FK, not two separate ones. Postgres now makes it impossible
        # for a document's tenant_id to disagree with its workspace's tenant_id.
        ForeignKeyConstraint(
            ["workspace_id", "tenant_id"],
            ["workspaces.id", "workspaces.tenant_id"],
            name="fk_documents_workspace_id_tenant_id_workspaces",
            ondelete="CASCADE",
        ),
    )

    # Denormalised from workspaces so every RLS policy is one indexed
    # column comparison. Kept honest by the composite FK above.
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)

    workspace: Mapped["Workspace"] = relationship(back_populates="documents")

    def __repr__(self) -> str:
        return f"<Document id={self.id} workspace_id={self.workspace_id} title={self.title!r}>"
