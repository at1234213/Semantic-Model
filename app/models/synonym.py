import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, UniqueConstraint, Uuid
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
    from app.models.dimension import Dimension
    from app.models.entity import Entity
    from app.models.metric import Metric


class Synonym(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """An alternate phrasing that resolves to one semantic object.

    "Turnover", "top line" and "sales" all point at the `revenue` metric. Free
    text, since a person's phrasing is not an identifier — it is matched during
    retrieval, never compiled.

    No synonym for a measure: measures are never named by the intent agent.
    """

    __tablename__ = "synonyms"
    __table_args__ = (
        tenant_unique("synonyms"),
        parent_fk("synonyms", "semantic_model_version_id", "semantic_model_versions"),
        parent_fk("synonyms", "entity_id", "entities"),
        parent_fk("synonyms", "dimension_id", "dimensions"),
        parent_fk("synonyms", "metric_id", "metrics"),
        UniqueConstraint(
            "semantic_model_version_id", "term", name="uq_synonyms_version_id_term"
        ),
        CheckConstraint(
            "(entity_id IS NOT NULL)::int + (dimension_id IS NOT NULL)::int "
            "+ (metric_id IS NOT NULL)::int = 1",
            name="exactly_one_target",
        ),
        CheckConstraint("length(trim(term)) > 0", name="term_present"),
    )

    semantic_model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    term: Mapped[str] = mapped_column(String(255))

    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, default=None
    )
    dimension_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, default=None
    )
    metric_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, default=None
    )

    entity: Mapped["Entity | None"] = relationship(
        foreign_keys=[entity_id], viewonly=True
    )
    dimension: Mapped["Dimension | None"] = relationship(
        foreign_keys=[dimension_id], viewonly=True
    )
    metric: Mapped["Metric | None"] = relationship(
        foreign_keys=[metric_id], viewonly=True
    )

    @property
    def target_id(self) -> uuid.UUID:
        return self.entity_id or self.dimension_id or self.metric_id  # type: ignore[return-value]

    def __repr__(self) -> str:
        return f"<Synonym {self.term!r} -> {self.target_id}>"
