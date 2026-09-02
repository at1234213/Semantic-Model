import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Index, Integer, Text, UniqueConstraint, Uuid, text
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
    from app.models.semantic_model import SemanticModel


class VersionStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class SemanticModelVersion(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """The unit of reproducibility. Every semantic object hangs off one of these.

    There is deliberately no `current_version_id` pointer on SemanticModel: it
    would be a circular foreign key and a second source of truth that could
    disagree with `status`. The published version is derived instead, enforced
    by the partial unique index below.
    """

    __tablename__ = "semantic_model_versions"
    __table_args__ = (
        tenant_unique("semantic_model_versions"),
        parent_fk("semantic_model_versions", "semantic_model_id", "semantic_models"),
        UniqueConstraint(
            "semantic_model_id",
            "version",
            name="uq_semantic_model_versions_semantic_model_id_version",
        ),
        # At most one published version per model. A CHECK cannot span rows;
        # a partial unique index can.
        Index(
            "uq_semantic_model_versions_one_published",
            "semantic_model_id",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    semantic_model_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[VersionStatus] = mapped_column(
        SAEnum(
            VersionStatus,
            name="version_status",
            native_enum=False,
            # Without this SQLAlchemy emits a bare VARCHAR and any string is
            # storable; with it Postgres enforces the three allowed values.
            create_constraint=True,
            length=32,
            # Store the value ('draft'), not the member name ('DRAFT'), which is
            # what SQLAlchemy would otherwise persist.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=VersionStatus.DRAFT,
        server_default=VersionStatus.DRAFT.value,
    )
    description: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), default=None)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    semantic_model: Mapped["SemanticModel"] = relationship(back_populates="versions")

    def __repr__(self) -> str:
        return (
            f"<SemanticModelVersion id={self.id} "
            f"model={self.semantic_model_id} v{self.version} {self.status}>"
        )
