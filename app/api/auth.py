"""Authentication.

Replaces the X-Tenant-ID header, which asked callers to declare who they were
and believed them. A bearer token is checked against a stored digest, and the
tenant comes from the row that matched — the caller no longer gets a say.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services import api_keys

UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="A valid API key is required",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass
class Principal:
    tenant_id: uuid.UUID
    api_key_id: uuid.UUID
    is_admin: bool


def get_principal(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UNAUTHENTICATED

    api_key = api_keys.verify(db, authorization.split(" ", 1)[1].strip())
    if api_key is None:
        raise UNAUTHENTICATED

    return Principal(
        tenant_id=api_key.tenant_id, api_key_id=api_key.id, is_admin=api_key.is_admin
    )


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        # 404-style opacity is not useful here: the caller is authenticated and
        # simply lacks the privilege, which they can act on.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires an admin API key",
        )
    return principal


def get_scoped_db(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Iterator[Session]:
    """A session with row-level security scoped to the authenticated tenant."""
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(principal.tenant_id)},
    )
    yield db
