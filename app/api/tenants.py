"""Tenant routes.

Deliberately NOT tenant-scoped: creating a tenant has no tenant to scope to.
Every route here requires an admin API key, which is what closes the gap left
open from Step 14 — before this, anything that could reach the port could
delete a tenant and cascade away its workspaces.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin
from app.core.db import get_db
from app.schemas.tenant import TenantCreate, TenantRead
from app.services import tenants as tenants_service

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.post("", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> TenantRead:
    try:
        tenant = tenants_service.create(db, name=payload.name)
        db.refresh(tenant)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A tenant named {payload.name!r} already exists",
        ) from None
    return tenant


@router.get("", response_model=list[TenantRead])
def list_tenants(
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[TenantRead]:
    return tenants_service.list_all(db, limit=limit, offset=offset)


@router.get("/{tenant_id}", response_model=TenantRead)
def get_tenant(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> TenantRead:
    tenant = tenants_service.get(db, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


@router.delete("/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> Response:
    tenant = tenants_service.get(db, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    tenants_service.delete(db, tenant)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
