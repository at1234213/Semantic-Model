import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, Text, UniqueConstraint, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.entity import IDENTIFIER_PATTERN
from app.models.mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    parent_fk,
    tenant_unique,
)

if TYPE_CHECKING:
    from app.models.entity import Entity


class DimensionDataType(enum.StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"


TIME_DATA_TYPES = (DimensionDataType.DATE, DimensionDataType.TIMESTAMP)


class TimeGranularity(enum.StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class Dimension(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """Something a question groups or filters by: `customer.state`, `order.order_date`.

    `expression` is a bare column of the owning entity, not arbitrary SQL. That
    is a deliberate restriction rather than an oversight: it keeps the compiler
    to identifier substitution, and anything more complex belongs in a view in
    the warehouse. Relaxing it later is safe; tightening it later would not be.
    """

    __tablename__ = "dimensions"
    __table_args__ = (
        tenant_unique("dimensions"),
        parent_fk("dimensions", "entity_id", "entities"),
        UniqueConstraint("entity_id", "name", name="uq_dimensions_entity_id_name"),
        CheckConstraint(f"name ~ '{IDENTIFIER_PATTERN}'", name="name_ident"),
        CheckConstraint(f"expression ~ '{IDENTIFIER_PATTERN}'", name="expression_ident"),
        # Granularity is meaningless on a non-temporal column, and allowing it
        # there would let a query ask to bucket a string by month.
        CheckConstraint(
            "granularity IS NULL OR data_type IN ('date', 'timestamp')",
            name="granularity_requires_time_type",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)

    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    expression: Mapped[str] = mapped_column(String(255))

    data_type: Mapped[DimensionDataType] = mapped_column(
        SAEnum(
            DimensionDataType,
            name="dimension_data_type",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=DimensionDataType.STRING,
        server_default=DimensionDataType.STRING.value,
    )

    granularity: Mapped[TimeGranularity | None] = mapped_column(
        SAEnum(
            TimeGranularity,
            name="time_granularity",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=None,
    )

    entity: Mapped["Entity"] = relationship(back_populates="dimensions")

    @property
    def is_time_dimension(self) -> bool:
        """Derived rather than stored: a separate flag could disagree with data_type."""
        return self.data_type in TIME_DATA_TYPES

    def __repr__(self) -> str:
        return f"<Dimension id={self.id} name={self.name!r} type={self.data_type}>"
