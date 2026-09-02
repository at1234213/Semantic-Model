import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin, parent_fk

if TYPE_CHECKING:
    from app.models.measure import Measure
    from app.models.metric import Metric


class MetricReference(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """One dependency edge of a metric, resolved from `${...}` at write time.

    Exactly one of ref_measure_id / ref_metric_id is set — the same exclusive
    foreign key shape as synonyms will use.
    """

    __tablename__ = "metric_references"
    __table_args__ = (
        parent_fk("metric_references", "metric_id", "metrics"),
        parent_fk("metric_references", "ref_measure_id", "measures"),
        parent_fk("metric_references", "ref_metric_id", "metrics"),
        CheckConstraint(
            "(ref_measure_id IS NULL) <> (ref_metric_id IS NULL)",
            name="exactly_one_target",
        ),
        CheckConstraint(
            "ref_metric_id IS NULL OR ref_metric_id <> metric_id",
            name="no_self_reference",
        ),
    )

    metric_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    ref_measure_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, default=None
    )
    ref_metric_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, default=None
    )

    metric: Mapped["Metric"] = relationship(
        back_populates="references", foreign_keys=[metric_id]
    )
    measure: Mapped["Measure | None"] = relationship(
        foreign_keys=[ref_measure_id], viewonly=True
    )
    referenced_metric: Mapped["Metric | None"] = relationship(
        foreign_keys=[ref_metric_id], viewonly=True
    )

    def __repr__(self) -> str:
        target = self.ref_measure_id or self.ref_metric_id
        return f"<MetricReference metric={self.metric_id} -> {target}>"
