import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
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
    from app.models.data_source import DataSource
    from app.models.entity import Entity
    from app.models.metric import Metric
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
        parent_fk("semantic_model_versions", "data_source_id", "data_sources"),
        # A draft may be half-assembled; a published version may not. One
        # semantic model version binds to exactly one data source, because the
        # compiler emits a single statement and SQL cannot join across
        # connections.
        CheckConstraint(
            "status <> 'published' OR data_source_id IS NOT NULL",
            name="published_has_source",
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
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, default=None
    )
    description: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), default=None)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    semantic_model: Mapped["SemanticModel"] = relationship(back_populates="versions")
    # viewonly: tenant_id participates in two composite FKs (to semantic_models
    # and to data_sources), so without this both relationships believe they own
    # writing it. Ownership belongs to semantic_model; data_source_id is set
    # directly as a plain column.
    data_source: Mapped["DataSource | None"] = relationship(
        back_populates="semantic_model_versions", viewonly=True
    )
    entities: Mapped[list["Entity"]] = relationship(
        back_populates="semantic_model_version",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    metrics: Mapped[list["Metric"]] = relationship(
        back_populates="semantic_model_version",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"<SemanticModelVersion id={self.id} "
            f"model={self.semantic_model_id} v{self.version} {self.status}>"
        )
