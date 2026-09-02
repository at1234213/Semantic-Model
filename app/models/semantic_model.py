import uuid
from typing import TYPE_CHECKING

from sqlalchemy import String, Text, UniqueConstraint, Uuid
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
    from app.models.semantic_model_version import SemanticModelVersion
    from app.models.workspace import Workspace


class SemanticModel(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """Identity only. Holds no definitions.

    Entities, measures, metrics and the rest belong to a *version*, so a
    conversation from months ago can still recompile the SQL it generated.
    """

    __tablename__ = "semantic_models"
    __table_args__ = (
        tenant_unique("semantic_models"),
        parent_fk("semantic_models", "workspace_id", "workspaces"),
        UniqueConstraint("workspace_id", "name", name="uq_semantic_models_workspace_id_name"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, default=None)

    workspace: Mapped["Workspace"] = relationship(back_populates="semantic_models")
    versions: Mapped[list["SemanticModelVersion"]] = relationship(
        back_populates="semantic_model",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SemanticModelVersion.version",
    )

    def __repr__(self) -> str:
        return f"<SemanticModel id={self.id} name={self.name!r}>"
