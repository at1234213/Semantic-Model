"""The question-answering endpoint."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import Principal, get_principal, get_scoped_db
from app.models.semantic_model import SemanticModel
from app.models.semantic_model_version import SemanticModelVersion
from app.schemas.ask import AskRequest, AskResponse
from app.services import audit
from app.services.pipeline import answer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ask", tags=["ask"])


@router.post("", response_model=AskResponse)
def ask(
    payload: AskRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_scoped_db),
) -> AskResponse:
    # One query rather than db.get(): a Session can serve db.get() straight from
    # its identity map without touching the database, which would step around
    # row-level security. A select always goes to Postgres and is filtered there.
    row = db.execute(
        select(SemanticModelVersion, SemanticModel.workspace_id)
        .join(SemanticModel, SemanticModel.id == SemanticModelVersion.semantic_model_id)
        .where(SemanticModelVersion.id == payload.semantic_model_version_id)
    ).first()
    if row is None:
        # RLS already hides other tenants' versions, so "not found" is accurate
        # rather than evasive.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Semantic model version not found",
        )
    version, workspace_id = row

    result = answer(
        db,
        payload.question,
        version_id=version.id,
        execute_query=payload.execute,
    )
    run = audit.record(
        db,
        result,
        tenant_id=principal.tenant_id,
        workspace_id=workspace_id,
        version_id=version.id,
        api_key_id=principal.api_key_id,
    )
    db.commit()

    logger.info(
        "ask.completed",
        extra={
            "run_id": str(run.id),
            "tenant_id": str(principal.tenant_id),
            "status": result.status,
            "attempts": result.attempts,
            "row_count": result.result.row_count if result.result else None,
        },
    )

    response = AskResponse(
        run_id=run.id,
        status=result.status,
        question=result.question,
        attempts=result.attempts,
        problems=result.problems,
    )
    if result.compiled is not None:
        response.sql = result.compiled.sql
        response.columns = result.compiled.columns
    if result.result is not None:
        response.rows = [list(row) for row in result.result.rows]
        response.row_count = result.result.row_count
        response.truncated = result.result.truncated
        response.duration_ms = result.result.duration_ms
    if result.analysis is not None:
        response.summary = result.analysis.headline()
    return response


@router.get("/runs", response_model=list[dict])
def list_runs(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_scoped_db),
    limit: int = 20,
) -> list[dict]:
    """Recent questions for the calling tenant. Scoped by RLS, not by a filter."""
    from app.models.query_run import QueryRun

    runs = db.scalars(
        select(QueryRun).order_by(QueryRun.created_at.desc()).limit(min(limit, 100))
    )
    return [
        {
            "id": str(run.id),
            "question": run.question,
            "status": str(run.status),
            "row_count": run.row_count,
            "created_at": run.created_at.isoformat(),
        }
        for run in runs
    ]
