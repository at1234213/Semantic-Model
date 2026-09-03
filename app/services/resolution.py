"""Turning an intent's phrases into semantic objects.

This is where the model's output stops being trusted and starts being checked.
Every phrase is looked up through hybrid retrieval and must land on an object of
the *expected kind* — a metric phrase that resolves to a dimension is not a
match, it is a mistake worth reporting.

Nothing here calls a model. Given the same intent and the same semantic model
version, it produces the same result.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.search_index import SearchObjectType
from app.services.embeddings import Embedder
from app.services.intent import Intent, IntentType
from app.services.retrieval import Hit, retrieve

# Below this, a top hit is a coincidence rather than a match. Tuned against the
# RRF scale: a single mid-ranked list contributes roughly 1/(60+n).
MIN_SCORE = 0.008


@dataclass
class Resolution:
    phrase: str
    expected: SearchObjectType
    hit: Hit | None = None
    alternatives: list[Hit] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.hit is not None

    @property
    def reason(self) -> str | None:
        if self.resolved:
            return None
        if self.alternatives:
            found = self.alternatives[0]
            return (
                f"{self.phrase!r} looks like {found.name!r}, which is a "
                f"{found.object_type}, not a {self.expected}"
            )
        return f"Nothing in this semantic model matches {self.phrase!r}"


@dataclass
class ResolvedIntent:
    intent: Intent
    metrics: list[Resolution] = field(default_factory=list)
    dimensions: list[Resolution] = field(default_factory=list)
    filter_fields: list[Resolution] = field(default_factory=list)

    @property
    def all_resolutions(self) -> list[Resolution]:
        return [*self.metrics, *self.dimensions, *self.filter_fields]

    @property
    def is_complete(self) -> bool:
        return self.intent.is_answerable and all(r.resolved for r in self.all_resolutions)

    @property
    def problems(self) -> list[str]:
        if self.intent.intent_type is IntentType.UNSUPPORTED:
            return [self.intent.note or "The question is not answerable from this model."]
        return [r.reason for r in self.all_resolutions if r.reason is not None]


def _resolve_one(
    db: Session,
    phrase: str,
    expected: SearchObjectType,
    *,
    version_id: uuid.UUID,
    embedder: Embedder | None,
) -> Resolution:
    hits = retrieve(db, phrase, version_id=version_id, limit=5, embedder=embedder)
    strong = [hit for hit in hits if hit.score >= MIN_SCORE]

    for hit in strong:
        if hit.object_type is expected:
            return Resolution(phrase=phrase, expected=expected, hit=hit,
                              alternatives=[h for h in strong if h is not hit])
    return Resolution(phrase=phrase, expected=expected, alternatives=strong)


def resolve(
    db: Session,
    intent: Intent,
    *,
    version_id: uuid.UUID,
    embedder: Embedder | None = None,
) -> ResolvedIntent:
    """Resolve every phrase in an intent against one semantic model version."""
    resolved = ResolvedIntent(intent=intent)
    if intent.intent_type is IntentType.UNSUPPORTED:
        return resolved

    resolved.metrics = [
        _resolve_one(db, phrase, SearchObjectType.METRIC, version_id=version_id,
                     embedder=embedder)
        for phrase in intent.metric_phrases
    ]
    resolved.dimensions = [
        _resolve_one(db, phrase, SearchObjectType.DIMENSION, version_id=version_id,
                     embedder=embedder)
        for phrase in intent.dimension_phrases
    ]
    resolved.filter_fields = [
        _resolve_one(db, filter_.field_phrase, SearchObjectType.DIMENSION,
                     version_id=version_id, embedder=embedder)
        for filter_ in intent.filters
    ]
    return resolved
