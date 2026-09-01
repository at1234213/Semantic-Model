"""Shared FastAPI dependencies."""

import uuid
from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db


def get_tenant_id(x_tenant_id: str | None = Header(default=None)) -> uuid.UUID:
    """Resolve the calling tenant from the X-Tenant-ID header.

    This is the single place tenant identity enters the application. Swapping
    it for a JWT claim later means changing this function and nothing else.
    """
    if x_tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-ID header is required",
        )
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-ID must be a valid UUID",
        ) from None


def get_tenant_db(
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> Iterator[Session]:
    """A session with the Postgres RLS policies scoped to the calling tenant.

    set_config(..., true) is transaction-local, the same as SET LOCAL, but it
    accepts a bind parameter where SET LOCAL cannot. It is discarded on commit,
    so every read must happen before the route commits.
    """
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
    yield db
