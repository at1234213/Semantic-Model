import enum
import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    parent_fk,
    tenant_unique,
)
from app.services.embeddings import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from app.models.semantic_model_version import SemanticModelVersion


class SearchObjectType(enum.StrEnum):
    ENTITY = "entity"
    DIMENSION = "dimension"
    METRIC = "metric"
    GLOSSARY_TERM = "glossary_term"


class SemanticSearchIndex(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """One searchable row per semantic object, for hybrid retrieval.

    Derived data, rebuilt wholesale for a version rather than reconciled. That
    is why `object_id` carries no foreign key: it points at four different
    tables, and a polymorphic FK cannot be expressed. A stale row resolves to
    nothing on lookup rather than corrupting anything, and deleting a version
    cascades the whole index away.

    Denormalised on purpose. Ranking three retrieval methods against each other
    is far simpler over one table than over four, and `search_text` folds in an
    object's synonyms so "turnover" finds the revenue metric.
    """

    __tablename__ = "semantic_search_index"
    __table_args__ = (
        tenant_unique("semantic_search_index"),
        parent_fk(
            "semantic_search_index", "semantic_model_version_id", "semantic_model_versions"
        ),
        UniqueConstraint(
            "semantic_model_version_id",
            "object_type",
            "object_id",
            name="uq_semantic_search_index_object",
        ),
        CheckConstraint("length(trim(search_text)) > 0", name="search_text_present"),
        CheckConstraint(
            "(embedding IS NULL) = (embedding_model IS NULL)", name="embedding_has_model"
        ),
        Index(
            "ix_semantic_search_index_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_semantic_search_index_fts", "search_vector", postgresql_using="gin"
        ),
    )

    semantic_model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)

    object_type: Mapped[SearchObjectType] = mapped_column(
        SAEnum(
            SearchObjectType,
            name="search_object_type",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        )
    )
    object_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)

    # The canonical name, kept separate so exact and synonym matching does not
    # have to pick it back out of search_text.
    name: Mapped[str] = mapped_column(String(255), index=True)
    search_text: Mapped[str] = mapped_column(Text)

    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', search_text)", persisted=True),
    )

    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), default=None
    )
    embedding_model: Mapped[str | None] = mapped_column(String(255), default=None)

    semantic_model_version: Mapped["SemanticModelVersion"] = relationship(
        back_populates="search_index"
    )

    def __repr__(self) -> str:
        return f"<SemanticSearchIndex {self.object_type}:{self.name!r}>"
