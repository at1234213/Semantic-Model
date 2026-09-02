import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, Text, UniqueConstraint, Uuid
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
    from app.models.business_rule import BusinessRule
    from app.models.dimension import Dimension
    from app.models.measure import Measure
    from app.models.semantic_model_version import SemanticModelVersion

# Every one of these values is interpolated into compiled SQL as an identifier,
# so the database refuses anything that is not identifier-shaped. This is the
# last line of defence behind application-level validation.
IDENTIFIER_PATTERN = "^[A-Za-z_][A-Za-z0-9_]*$"


class EntityKind(enum.StrEnum):
    FACT = "fact"
    DIMENSION = "dimension"


class Entity(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """A business object bound to one physical table in the source warehouse.

    `source_schema` / `source_table` name a table in the *customer's* database,
    not this one, so their existence cannot be checked against local
    information_schema. They are shape-validated here and resolved at compile
    time.
    """

    __tablename__ = "entities"
    __table_args__ = (
        tenant_unique("entities"),
        parent_fk("entities", "semantic_model_version_id", "semantic_model_versions"),
        UniqueConstraint(
            "semantic_model_version_id", "name", name="uq_entities_version_id_name"
        ),
        # Target for measures' three-column foreign key, which pins a measure's
        # entity, version and tenant together in one constraint.
        UniqueConstraint(
            "id", "semantic_model_version_id", "tenant_id", name="id_version_tenant"
        ),
        CheckConstraint(
            f"source_schema ~ '{IDENTIFIER_PATTERN}'", name="source_schema_ident"
        ),
        CheckConstraint(
            f"source_table ~ '{IDENTIFIER_PATTERN}'", name="source_table_ident"
        ),
        CheckConstraint(
            f"primary_key ~ '{IDENTIFIER_PATTERN}'", name="primary_key_ident"
        ),
        CheckConstraint(f"name ~ '{IDENTIFIER_PATTERN}'", name="name_ident"),
    )

    semantic_model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)

    # Semantic side: what a person or the retrieval layer calls it.
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    # Physical side: where it actually lives.
    source_schema: Mapped[str] = mapped_column(String(255), default="public")
    source_table: Mapped[str] = mapped_column(String(255))
    primary_key: Mapped[str] = mapped_column(String(255), default="id")

    kind: Mapped[EntityKind] = mapped_column(
        SAEnum(
            EntityKind,
            name="entity_kind",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=EntityKind.DIMENSION,
        server_default=EntityKind.DIMENSION.value,
    )

    semantic_model_version: Mapped["SemanticModelVersion"] = relationship(
        back_populates="entities"
    )
    dimensions: Mapped[list["Dimension"]] = relationship(
        back_populates="entity",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    measures: Mapped[list["Measure"]] = relationship(
        back_populates="entity",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    business_rules: Mapped[list["BusinessRule"]] = relationship(
        back_populates="entity", viewonly=True
    )

    @property
    def qualified_table(self) -> str:
        """The compiler's view: schema-qualified physical table."""
        return f"{self.source_schema}.{self.source_table}"

    def __repr__(self) -> str:
        return f"<Entity id={self.id} name={self.name!r} -> {self.qualified_table}>"
