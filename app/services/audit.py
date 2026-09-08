"""Recording what was asked and what SQL it produced.

Deliberately records no result rows and no parameter values. Those are the
customer's data; an audit log should not become a second copy of it. The
statement text and the parameter count are enough to reconstruct what ran.
"""

import uuid

from sqlalchemy.orm import Session

from app.models.query_run import QueryRun, RunStatus
from app.services.pipeline import PipelineResult


def record(
    db: Session,
    result: PipelineResult,
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    version_id: uuid.UUID,
    api_key_id: uuid.UUID | None = None,
) -> QueryRun:
    if not result.ok:
        status = RunStatus.FAILED
    elif result.result is not None:
        status = RunStatus.EXECUTED
    else:
        status = RunStatus.COMPILED

    run = QueryRun(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        semantic_model_version_id=version_id,
        api_key_id=api_key_id,
        question=result.question,
        status=status,
        intent=result.intent.model_dump(mode="json") if result.intent else None,
        compiled_sql=result.compiled.sql if result.compiled else None,
        parameter_count=len(result.compiled.parameters) if result.compiled else 0,
        attempts=result.attempts,
        row_count=result.result.row_count if result.result else None,
        duration_ms=result.result.duration_ms if result.result else None,
        problems=result.problems or None,
    )
    db.add(run)
    db.flush()
    return run
