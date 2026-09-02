import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.data_source import DataSource
    from app.models.document import Document
    from app.models.semantic_model import SemanticModel
    from app.models.tenant import Tenant


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant's analytics workspace: the scope that will own a semantic
    model, its documents, and its query history.
    """

    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_workspaces_tenant_id_name"),
        # Target for child tables' composite foreign keys. Redundant with the
        # primary key on its own, but a composite FK needs a matching unique
        # constraint on exactly these two columns.
        UniqueConstraint("id", "tenant_id", name="uq_workspaces_id_tenant_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))

    tenant: Mapped["Tenant"] = relationship(back_populates="workspaces")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    semantic_models: Mapped[list["SemanticModel"]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    data_sources: Mapped[list["DataSource"]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Workspace id={self.id} tenant_id={self.tenant_id} name={self.name!r}>"
