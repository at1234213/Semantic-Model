import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, SmallInteger, String, UniqueConstraint, Uuid
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
    from app.models.relationship import Relationship


class RelationshipJoinKey(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """One column pair of a join edge.

    A child table rather than a text column so composite joins work and the
    compiler emits identifiers it looked up, never a string it parsed.
    """

    __tablename__ = "relationship_join_keys"
    __table_args__ = (
        tenant_unique("relationship_join_keys"),
        parent_fk("relationship_join_keys", "relationship_id", "relationships"),
        UniqueConstraint(
            "relationship_id", "ordinal", name="uq_relationship_join_keys_relationship_ordinal"
        ),
        CheckConstraint(f"from_column ~ '{IDENTIFIER_PATTERN}'", name="from_column_ident"),
        CheckConstraint(f"to_column ~ '{IDENTIFIER_PATTERN}'", name="to_column_ident"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )

    relationship_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    from_column: Mapped[str] = mapped_column(String(255))
    to_column: Mapped[str] = mapped_column(String(255))
    ordinal: Mapped[int] = mapped_column(SmallInteger, default=0)

    relationship: Mapped["Relationship"] = relationship(back_populates="join_keys")

    def __repr__(self) -> str:
        return f"<RelationshipJoinKey {self.from_column} = {self.to_column}>"
