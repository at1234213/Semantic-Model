"""Workspace routes. Every route is scoped to the tenant in X-Tenant-ID."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_db, get_tenant_id
from app.schemas.workspace import WorkspaceCreate, WorkspaceRead
from app.services import tenants as tenants_service
from app.services import workspaces as workspaces_service

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceCreate,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    db: Session = Depends(get_tenant_db),
) -> WorkspaceRead:
    # Checked explicitly so an unknown tenant is a clear 404 rather than a
    # foreign-key error surfacing as a 500.
    if tenants_service.get(db, tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    try:
        workspace = workspaces_service.create(db, tenant_id=tenant_id, name=payload.name)
        # Refresh before commit: the tenant setting is transaction-local, so a
        # post-commit read would run unscoped and RLS would hide the row.
        db.refresh(workspace)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This tenant already has a workspace named {payload.name!r}",
        ) from None
    return workspace


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    db: Session = Depends(get_tenant_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[WorkspaceRead]:
    return workspaces_service.list_for_tenant(db, tenant_id=tenant_id, limit=limit, offset=offset)


@router.get("/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    db: Session = Depends(get_tenant_db),
) -> WorkspaceRead:
    workspace = workspaces_service.get(db, tenant_id=tenant_id, workspace_id=workspace_id)
    if workspace is None:
        # 404 rather than 403: another tenant's workspace must be
        # indistinguishable from one that does not exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return workspace


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    db: Session = Depends(get_tenant_db),
) -> Response:
    workspace = workspaces_service.get(db, tenant_id=tenant_id, workspace_id=workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    workspaces_service.delete(db, workspace)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
