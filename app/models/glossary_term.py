import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, Text, UniqueConstraint, Uuid
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
    from app.models.semantic_model_version import SemanticModelVersion


class GlossaryTerm(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """Prose a person or the LLM reads. Never compiled into SQL.

    `term` is free text rather than an identifier — "active customer" has a
    space — precisely because nothing here reaches the compiler.
    """

    __tablename__ = "glossary_terms"
    __table_args__ = (
        tenant_unique("glossary_terms"),
        parent_fk("glossary_terms", "semantic_model_version_id", "semantic_model_versions"),
        UniqueConstraint(
            "semantic_model_version_id", "term", name="uq_glossary_terms_version_id_term"
        ),
        CheckConstraint("length(trim(term)) > 0", name="term_present"),
        CheckConstraint("length(trim(definition)) > 0", name="definition_present"),
    )

    semantic_model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    term: Mapped[str] = mapped_column(String(255))
    definition: Mapped[str] = mapped_column(Text)

    semantic_model_version: Mapped["SemanticModelVersion"] = relationship(
        back_populates="glossary_terms"
    )

    def __repr__(self) -> str:
        return f"<GlossaryTerm {self.term!r}>"
