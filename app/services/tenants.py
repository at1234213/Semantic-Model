"""Tenant persistence.

Kept out of the route handlers so the LangGraph nodes in Step 25 can call the
same functions without going through HTTP. These functions flush but never
commit: transaction boundaries belong to the caller.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Tenant


def create(db: Session, *, name: str) -> Tenant:
    tenant = Tenant(name=name)
    db.add(tenant)
    db.flush()
    return tenant


def get(db: Session, tenant_id: uuid.UUID) -> Tenant | None:
    return db.get(Tenant, tenant_id)


def list_all(db: Session, *, limit: int = 50, offset: int = 0) -> list[Tenant]:
    stmt = select(Tenant).order_by(Tenant.created_at).limit(limit).offset(offset)
    return list(db.scalars(stmt))


def delete(db: Session, tenant: Tenant) -> None:
    db.delete(tenant)
