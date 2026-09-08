import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    semantic_model_version_id: uuid.UUID
    execute: bool = True


class AskResponse(BaseModel):
    run_id: uuid.UUID
    status: str
    question: str
    attempts: int
    problems: list[str] = Field(default_factory=list)

    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int | None = None
    truncated: bool = False
    duration_ms: float | None = None
    summary: str | None = None
