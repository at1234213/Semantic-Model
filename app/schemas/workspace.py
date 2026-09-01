import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceCreate(BaseModel):
    """Deliberately has no tenant_id: the tenant comes from the X-Tenant-ID
    header, never the request body, so a client cannot create a workspace
    inside someone else's tenant.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime
