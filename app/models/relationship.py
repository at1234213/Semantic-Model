import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.entity import IDENTIFIER_PATTERN
from app.models.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin, tenant_unique

if TYPE_CHECKING:
    from app.models.entity import Entity
    from app.models.relationship_join_key import RelationshipJoinKey


class JoinType(enum.StrEnum):
    INNER = "inner"
    LEFT = "left"


class Cardinality(enum.StrEnum):
    MANY_TO_ONE = "many_to_one"
    ONE_TO_MANY = "one_to_many"
    ONE_TO_ONE = "one_to_one"


class Relationship(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """One join edge between two entities.

    Collectively these are the graph the join-path resolver walks when a metric
    and a filter live on different entities. There is no `join_condition` text
    column on purpose: the resolver traverses edges, and a string cannot be
    traversed without parsing it.

    At most one edge per ordered pair. Two roles over the same physical table —
    billing and shipping addresses, say — are modelled as two entities sharing a
    `source_table`, which keeps the graph unambiguous.
    """

    __tablename__ = "relationships"
    __table_args__ = (
        tenant_unique("relationships"),
        # Three-column keys pin both endpoints to the same version and tenant,
        # so an edge cannot span two versions of a model.
        ForeignKeyConstraint(
            ["from_entity_id", "semantic_model_version_id", "tenant_id"],
            ["entities.id", "entities.semantic_model_version_id", "entities.tenant_id"],
            name="fk_relationships_from_entity_id",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["to_entity_id", "semantic_model_version_id", "tenant_id"],
            ["entities.id", "entities.semantic_model_version_id", "entities.tenant_id"],
            name="fk_relationships_to_entity_id",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "semantic_model_version_id",
            "from_entity_id",
            "to_entity_id",
            name="uq_relationships_version_from_to",
        ),
        UniqueConstraint(
            "semantic_model_version_id", "name", name="uq_relationships_version_id_name"
        ),
        CheckConstraint(f"name ~ '{IDENTIFIER_PATTERN}'", name="name_ident"),
        CheckConstraint("from_entity_id <> to_entity_id", name="no_self_join"),
    )

    semantic_model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    from_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    to_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)

    name: Mapped[str] = mapped_column(String(255))

    join_type: Mapped[JoinType] = mapped_column(
        SAEnum(
            JoinType,
            name="join_type",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=JoinType.INNER,
        server_default=JoinType.INNER.value,
    )
    cardinality: Mapped[Cardinality] = mapped_column(
        SAEnum(
            Cardinality,
            name="cardinality",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=Cardinality.MANY_TO_ONE,
        server_default=Cardinality.MANY_TO_ONE.value,
    )

    # viewonly: both endpoints write semantic_model_version_id and tenant_id, so
    # without it two relationships would claim ownership of the same columns.
    from_entity: Mapped["Entity"] = relationship(
        foreign_keys=[from_entity_id], viewonly=True
    )
    to_entity: Mapped["Entity"] = relationship(foreign_keys=[to_entity_id], viewonly=True)

    join_keys: Mapped[list["RelationshipJoinKey"]] = relationship(
        back_populates="relationship",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RelationshipJoinKey.ordinal",
    )

    def __repr__(self) -> str:
        return f"<Relationship {self.name!r} {self.from_entity_id} -> {self.to_entity_id}>"
