"""Hybrid retrieval over the semantic layer.

Three ways of finding the same thing, fused into one ranking:

    exact     the name, or one of its synonyms, typed verbatim
    lexical   Postgres full-text over name, description and synonyms
    vector    cosine similarity over embeddings

Fusion is Reciprocal Rank Fusion rather than a weighted sum of scores. A cosine
distance and a ts_rank are not on the same scale, and normalising them means
inventing weights that quietly decide the outcome. RRF uses only rank position,
so each method gets a vote it earned rather than a number that happens to be
large.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.entity import Entity
from app.models.glossary_term import GlossaryTerm
from app.models.metric import Metric
from app.models.search_index import SearchObjectType, SemanticSearchIndex
from app.models.semantic_model_version import SemanticModelVersion
from app.models.synonym import Synonym
from app.services.embeddings import Embedder, get_embedder

# Standard RRF constant. Large enough that the top few ranks are close together,
# so a single method cannot dominate on its own.
RRF_K = 60
# Exact matches are not guesses. Ranked as their own list, they reliably win.
EXACT_WEIGHT = 2.0

# Nearest-neighbour search returns its top N however far away they are. On a
# small model that means every query "matches" every object, and RRF then gives
# the noise a plausible-looking score. Unit vectors are at distance 1.0 when
# orthogonal, so anything at or past this is unrelated, not merely a weak match.
MAX_VECTOR_DISTANCE = 0.9


@dataclass
class Hit:
    object_type: SearchObjectType
    object_id: uuid.UUID
    name: str
    score: float
    matched_by: list[str] = field(default_factory=list)


def _text_for(name: str, display_name: str | None, description: str | None,
              synonyms: list[str]) -> str:
    parts = [name.replace("_", " "), display_name or "", description or "", *synonyms]
    return " ".join(part for part in parts if part).strip()


def rebuild_search_index(
    db: Session, version_id: uuid.UUID, *, embedder: Embedder | None = None
) -> int:
    """Rebuild every searchable row for one semantic model version.

    Wholesale rather than incremental: the index is derived, and reconciling it
    row by row is how it drifts.
    """
    embedder = embedder or get_embedder()

    version = db.get(SemanticModelVersion, version_id)
    if version is None:
        return 0

    synonyms_by_target: dict[uuid.UUID, list[str]] = {}
    for synonym in db.scalars(
        select(Synonym).where(Synonym.semantic_model_version_id == version_id)
    ):
        target = synonym.entity_id or synonym.dimension_id or synonym.metric_id
        synonyms_by_target.setdefault(target, []).append(synonym.term)

    rows: list[SemanticSearchIndex] = []

    def add(object_type: SearchObjectType, obj_id: uuid.UUID, name: str,
            display_name: str | None, description: str | None) -> None:
        text = _text_for(name, display_name, description, synonyms_by_target.get(obj_id, []))
        if text:
            rows.append(
                SemanticSearchIndex(
                    tenant_id=version.tenant_id,
                    semantic_model_version_id=version_id,
                    object_type=object_type,
                    object_id=obj_id,
                    name=name,
                    search_text=text,
                )
            )

    for entity in db.scalars(
        select(Entity).where(Entity.semantic_model_version_id == version_id)
    ):
        add(SearchObjectType.ENTITY, entity.id, entity.name, entity.display_name,
            entity.description)
        for dimension in entity.dimensions:
            add(SearchObjectType.DIMENSION, dimension.id, dimension.name,
                dimension.display_name, dimension.description)

    for metric in db.scalars(
        select(Metric).where(Metric.semantic_model_version_id == version_id)
    ):
        add(SearchObjectType.METRIC, metric.id, metric.name, metric.display_name,
            metric.description)

    for term in db.scalars(
        select(GlossaryTerm).where(GlossaryTerm.semantic_model_version_id == version_id)
    ):
        add(SearchObjectType.GLOSSARY_TERM, term.id, term.term, None, term.definition)

    for existing in list(version.search_index):
        db.delete(existing)
    db.flush()

    if rows:
        for row, vector in zip(rows, embedder.embed([r.search_text for r in rows]),
                               strict=True):
            row.embedding = vector
            row.embedding_model = embedder.name
        db.add_all(rows)
    db.flush()
    db.refresh(version, ["search_index"])
    return len(rows)


def _rank(rows: list[SemanticSearchIndex]) -> dict[uuid.UUID, int]:
    return {row.id: position for position, row in enumerate(rows, start=1)}


def _exact(db: Session, query: str, version_id: uuid.UUID,
           limit: int) -> list[SemanticSearchIndex]:
    normalised = query.strip().lower()
    if not normalised:
        return []
    statement = (
        select(SemanticSearchIndex)
        .where(
            SemanticSearchIndex.semantic_model_version_id == version_id,
            or_(
                func.lower(SemanticSearchIndex.name) == normalised,
                func.lower(SemanticSearchIndex.name) == normalised.replace(" ", "_"),
            ),
        )
        .limit(limit)
    )
    return list(db.scalars(statement))


def _lexical(db: Session, query: str, version_id: uuid.UUID,
             limit: int) -> list[SemanticSearchIndex]:
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank(SemanticSearchIndex.search_vector, tsquery)
    statement = (
        select(SemanticSearchIndex)
        .where(
            SemanticSearchIndex.semantic_model_version_id == version_id,
            SemanticSearchIndex.search_vector.op("@@")(tsquery),
        )
        .order_by(rank.desc())
        .limit(limit)
    )
    return list(db.scalars(statement))


def _vector(db: Session, query: str, version_id: uuid.UUID, limit: int,
            embedder: Embedder) -> list[SemanticSearchIndex]:
    [vector] = embedder.embed([query])
    if not any(vector):
        return []  # a query with no tokens has no direction to search along

    distance = SemanticSearchIndex.embedding.cosine_distance(vector)
    statement = (
        select(SemanticSearchIndex)
        .where(
            SemanticSearchIndex.semantic_model_version_id == version_id,
            SemanticSearchIndex.embedding.is_not(None),
            SemanticSearchIndex.embedding_model == embedder.name,
            distance < MAX_VECTOR_DISTANCE,
        )
        .order_by(distance)
        .limit(limit)
    )
    return list(db.scalars(statement))


def retrieve(
    db: Session,
    query: str,
    *,
    version_id: uuid.UUID,
    limit: int = 10,
    candidates_per_method: int = 20,
    embedder: Embedder | None = None,
) -> list[Hit]:
    """Rank semantic objects for a question, fusing all three methods."""
    embedder = embedder or get_embedder()

    lists = {
        "exact": (_exact(db, query, version_id, candidates_per_method), EXACT_WEIGHT),
        "lexical": (_lexical(db, query, version_id, candidates_per_method), 1.0),
        "vector": (_vector(db, query, version_id, candidates_per_method, embedder), 1.0),
    }

    scores: dict[uuid.UUID, float] = {}
    matched: dict[uuid.UUID, list[str]] = {}
    rows_by_id: dict[uuid.UUID, SemanticSearchIndex] = {}

    for method, (rows, weight) in lists.items():
        for row_id, position in _rank(rows).items():
            scores[row_id] = scores.get(row_id, 0.0) + weight / (RRF_K + position)
            matched.setdefault(row_id, []).append(method)
        rows_by_id.update({row.id: row for row in rows})

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [
        Hit(
            object_type=rows_by_id[row_id].object_type,
            object_id=rows_by_id[row_id].object_id,
            name=rows_by_id[row_id].name,
            score=score,
            matched_by=matched[row_id],
        )
        for row_id, score in ordered
    ]
