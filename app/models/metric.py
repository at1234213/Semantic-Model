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
    from app.models.metric_reference import MetricReference
    from app.models.semantic_model_version import SemanticModelVersion

# Arithmetic over ${references} only. Deliberately admits no SQL functions, no
# quotes and no commas, which is what makes the grammar small enough to parse
# exhaustively and impossible to express an injection in.
METRIC_EXPRESSION_CHARSET = r"^[A-Za-z0-9_ ${}+*/().-]+$"


class MetricFormat(enum.StrEnum):
    NUMBER = "number"
    CURRENCY = "currency"
    PERCENT = "percent"


class Metric(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """What a person actually asks for.

    The expression composes measures and other metrics by name. Dependencies are
    parsed once at write time into `metric_references`, so resolution and cycle
    detection are graph walks rather than a regex on every query.
    """

    __tablename__ = "metrics"
    __table_args__ = (
        tenant_unique("metrics"),
        parent_fk("metrics", "semantic_model_version_id", "semantic_model_versions"),
        UniqueConstraint(
            "semantic_model_version_id", "name", name="uq_metrics_version_id_name"
        ),
        CheckConstraint(f"name ~ '{IDENTIFIER_PATTERN}'", name="name_ident"),
        CheckConstraint(
            f"expression ~ '{METRIC_EXPRESSION_CHARSET}' AND expression NOT LIKE '%--%'",
            name="expression_charset",
        ),
    )

    semantic_model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)

    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    expression: Mapped[str] = mapped_column(String(1000))

    format: Mapped[MetricFormat] = mapped_column(
        SAEnum(
            MetricFormat,
            name="metric_format",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=MetricFormat.NUMBER,
        server_default=MetricFormat.NUMBER.value,
    )

    semantic_model_version: Mapped["SemanticModelVersion"] = relationship(
        back_populates="metrics"
    )
    references: Mapped[list["MetricReference"]] = relationship(
        back_populates="metric",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="MetricReference.metric_id",
    )

    def __repr__(self) -> str:
        return f"<Metric id={self.id} {self.name!r}={self.expression!r}>"
