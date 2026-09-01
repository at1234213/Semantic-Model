"""Shared FastAPI dependencies."""

import uuid

from fastapi import Header, HTTPException, status


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
