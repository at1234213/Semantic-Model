"""Workspace persistence.

Every read is filtered by tenant_id at the application level. Step 14 adds
row-level security in Postgres on top, so isolation is enforced twice by two
independent mechanisms.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Workspace


def create(db: Session, *, tenant_id: uuid.UUID, name: str) -> Workspace:
    workspace = Workspace(tenant_id=tenant_id, name=name)
    db.add(workspace)
    db.flush()
    return workspace


def get(db: Session, *, tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> Workspace | None:
    """Scoped by tenant on purpose: a workspace belonging to another tenant
    must be indistinguishable from one that does not exist.
    """
    stmt = select(Workspace).where(
        Workspace.id == workspace_id,
        Workspace.tenant_id == tenant_id,
    )
    return db.scalars(stmt).first()


def list_for_tenant(
    db: Session, *, tenant_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[Workspace]:
    stmt = (
        select(Workspace)
        .where(Workspace.tenant_id == tenant_id)
        .order_by(Workspace.created_at)
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt))


def delete(db: Session, workspace: Workspace) -> None:
    db.delete(workspace)
