import enum
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, Text, UniqueConstraint, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.entity import IDENTIFIER_PATTERN
from app.models.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin, tenant_unique

if TYPE_CHECKING:
    from app.models.entity import Entity


class FilterOperator(enum.StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


NULLARY_OPERATORS = (FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL)


class BusinessRule(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """A filter the compiler injects into every query touching an entity.

    "Always exclude internal test accounts" lives here rather than in each
    question.

    Stored as column / operator / value rather than as a SQL predicate string.
    A predicate needs literals, and literals in a text column would be the one
    place in the semantic layer where a value gets interpolated into SQL.
    Structured, the value becomes a bind parameter and the operator maps to a
    fixed set of comparisons. It is also the same shape the Semantic Query IR
    needs for user-supplied filters in Step 26.
    """

    __tablename__ = "business_rules"
    __table_args__ = (
        tenant_unique("business_rules"),
        ForeignKeyConstraint(
            ["entity_id", "semantic_model_version_id", "tenant_id"],
            ["entities.id", "entities.semantic_model_version_id", "entities.tenant_id"],
            name="fk_business_rules_entity_id",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "semantic_model_version_id", "name", name="uq_business_rules_version_id_name"
        ),
        CheckConstraint(f"name ~ '{IDENTIFIER_PATTERN}'", name="name_ident"),
        CheckConstraint(f"column_name ~ '{IDENTIFIER_PATTERN}'", name="column_ident"),
        # is_null / is_not_null take no operand; every other operator needs one.
        #
        # JSONB has two kinds of empty: SQL NULL and the JSON value `null`.
        # none_as_null on the column makes Python None store as SQL NULL, and
        # jsonb_typeof catches a JSON null arriving through raw SQL. Checking
        # only `value IS NULL` silently accepted a valueless `eq` rule.
        CheckConstraint(
            "(operator IN ('is_null', 'is_not_null')) "
            "= (value IS NULL OR jsonb_typeof(value) = 'null')",
            name="value_matches_operator",
        ),
    )

    semantic_model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)

    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, default=None)

    column_name: Mapped[str] = mapped_column(String(255))
    operator: Mapped[FilterOperator] = mapped_column(
        SAEnum(
            FilterOperator,
            name="filter_operator",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        )
    )
    value: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True), default=None)

    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")

    entity: Mapped["Entity"] = relationship(back_populates="business_rules", viewonly=True)

    def __repr__(self) -> str:
        return f"<BusinessRule {self.name!r} {self.column_name} {self.operator} {self.value!r}>"
