import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class OrganizationBase(BaseModel):
    login: str
    name: str | None = None
    avatar_url: str | None = None


class OrganizationResponse(OrganizationBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    github_org_id: int
    installation_id: int | None = None
    sync_status: str
    owner_id: uuid.UUID
    created_at: datetime


class OrganizationList(BaseModel):
    organizations: list[OrganizationResponse]
    total: int
