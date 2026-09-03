"""Finding a path through the relationship graph.

A question whose metric lives on `orders` and whose filter lives on `customers`
needs the edge between them. This walks the graph the modeller declared, in
breadth-first order, and returns the smallest tree connecting everything the
query touches.

Nothing here guesses. If two entities are not connected by declared
relationships, that is reported — inventing a join is how a query returns a
plausible number computed from a cartesian product.
"""

import uuid
from collections import deque
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.entity import Entity
from app.models.relationship import Relationship


class JoinPathError(ValueError):
    """The required entities are not connected by declared relationships."""


@dataclass(frozen=True)
class JoinStep:
    """One JOIN: bring `entity` in, joined to `to_entity` already in the tree."""

    relationship: Relationship
    entity: Entity
    to_entity: Entity
    # True when walking the edge against the direction it was declared in.
    against_declared_direction: bool

    @property
    def fans_out(self) -> bool:
        """Does adding this entity multiply the rows already in the join?

        Walking many -> one adds at most one row per existing row. Walking
        one -> many multiplies them, which silently double-counts every
        aggregate computed on the other side.
        """
        cardinality = str(self.relationship.cardinality)
        if cardinality == "one_to_one":
            return False
        if cardinality == "many_to_one":
            return self.against_declared_direction
        return not self.against_declared_direction  # one_to_many


@dataclass
class JoinPlan:
    root: Entity
    steps: list[JoinStep]

    @property
    def entities(self) -> list[Entity]:
        return [self.root, *(step.entity for step in self.steps)]


def _load_graph(
    db: Session, version_id: uuid.UUID
) -> tuple[dict[uuid.UUID, Entity], dict[uuid.UUID, list[tuple[Relationship, uuid.UUID]]]]:
    entities = {
        entity.id: entity
        for entity in db.scalars(
            select(Entity).where(Entity.semantic_model_version_id == version_id)
        )
    }
    adjacency: dict[uuid.UUID, list[tuple[Relationship, uuid.UUID]]] = {
        entity_id: [] for entity_id in entities
    }
    for relationship in db.scalars(
        select(Relationship)
        .where(Relationship.semantic_model_version_id == version_id)
        .options(selectinload(Relationship.join_keys))
    ):
        # Traversable in both directions; the direction is recorded on the step
        # so the compiler knows which side each join key belongs to.
        adjacency[relationship.from_entity_id].append(
            (relationship, relationship.to_entity_id)
        )
        adjacency[relationship.to_entity_id].append(
            (relationship, relationship.from_entity_id)
        )
    return entities, adjacency


def resolve_join_path(
    db: Session, *, version_id: uuid.UUID, required_entity_ids: list[uuid.UUID],
    root_entity_id: uuid.UUID,
) -> JoinPlan:
    """Smallest tree of declared relationships connecting every required entity."""
    entities, adjacency = _load_graph(db, version_id)

    missing = [str(eid) for eid in {*required_entity_ids, root_entity_id}
               if eid not in entities]
    if missing:
        raise JoinPathError(f"Entities not present in this model version: {missing}")

    needed = set(required_entity_ids) | {root_entity_id}
    if needed == {root_entity_id}:
        return JoinPlan(root=entities[root_entity_id], steps=[])

    # Breadth-first from the root, recording how each entity was first reached.
    parent: dict[uuid.UUID, tuple[Relationship, uuid.UUID]] = {}
    seen = {root_entity_id}
    frontier = deque([root_entity_id])
    while frontier:
        current = frontier.popleft()
        for relationship, neighbour in adjacency[current]:
            if neighbour not in seen:
                seen.add(neighbour)
                parent[neighbour] = (relationship, current)
                frontier.append(neighbour)

    unreachable = sorted(entities[eid].name for eid in needed if eid not in seen)
    if unreachable:
        raise JoinPathError(
            f"{entities[root_entity_id].name!r} has no declared relationship path to: "
            f"{', '.join(unreachable)}. Add a relationship, or the question cannot be "
            "answered without inventing a join."
        )

    # Walk back from each needed entity to the root, collecting the edges used.
    ordered: list[uuid.UUID] = []
    included = {root_entity_id}
    for entity_id in needed:
        chain: list[uuid.UUID] = []
        cursor = entity_id
        while cursor not in included:
            chain.append(cursor)
            cursor = parent[cursor][1]
        ordered.extend(reversed(chain))
        included.update(chain)

    steps = [
        JoinStep(
            relationship=parent[entity_id][0],
            entity=entities[entity_id],
            to_entity=entities[parent[entity_id][1]],
            # We arrived at `entity_id`; if it is the edge's from_entity we
            # walked the edge backwards.
            against_declared_direction=(
                parent[entity_id][0].from_entity_id == entity_id
            ),
        )
        for entity_id in ordered
    ]
    return JoinPlan(root=entities[root_entity_id], steps=steps)
