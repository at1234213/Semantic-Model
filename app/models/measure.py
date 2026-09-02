import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, Text, UniqueConstraint, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.entity import IDENTIFIER_PATTERN
from app.models.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin, tenant_unique

if TYPE_CHECKING:
    from app.models.entity import Entity

# Bare columns, numbers and arithmetic. No function calls: the aggregation is a
# separate column, so `SUM(...)` never appears in an expression.
MEASURE_EXPRESSION_CHARSET = "^[A-Za-z0-9_ +*/().-]+$"


class Aggregation(enum.StrEnum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class Measure(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """An atomic aggregation over one entity's columns.

    Never named by the intent agent — it is reached through a metric. The
    expression uses unqualified columns of the owning entity, so cross-entity
    reference is impossible by construction and the compiler only has to prefix
    the entity's source table.

    Carries `semantic_model_version_id` so measure names are unique across a
    whole version, which is what lets a metric say `${gross_revenue}` without
    qualifying it. The three-column foreign key below guarantees that version
    agrees with the entity's.
    """

    __tablename__ = "measures"
    __table_args__ = (
        tenant_unique("measures"),
        # One FK, three columns: pins entity, version and tenant together so a
        # measure cannot reference an entity from a different version.
        ForeignKeyConstraint(
            ["entity_id", "semantic_model_version_id", "tenant_id"],
            [
                "entities.id",
                "entities.semantic_model_version_id",
                "entities.tenant_id",
            ],
            name="fk_measures_entity_id",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "semantic_model_version_id", "name", name="uq_measures_version_id_name"
        ),
        CheckConstraint(f"name ~ '{IDENTIFIER_PATTERN}'", name="name_ident"),
        CheckConstraint(
            f"expression ~ '{MEASURE_EXPRESSION_CHARSET}' AND expression NOT LIKE '%--%'",
            name="expression_charset",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    semantic_model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)

    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, default=None)

    aggregation: Mapped[Aggregation] = mapped_column(
        SAEnum(
            Aggregation,
            name="aggregation",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        )
    )
    expression: Mapped[str] = mapped_column(String(1000))

    # No relationship to SemanticModelVersion: the foreign key runs through
    # entities, so the version is reached as `measure.entity.semantic_model_version`.
    # Bulk loading for a whole version is a service query, not a relationship.
    entity: Mapped["Entity"] = relationship(back_populates="measures")

    def __repr__(self) -> str:
        return f"<Measure id={self.id} {self.name!r}={self.aggregation}({self.expression})>"
