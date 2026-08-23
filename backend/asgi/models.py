"""Pydantic models exposed by the FastAPI and OpenAPI boundaries."""
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class CredentialsRequest(ApiModel):
    username: str | None = None
    password: str | None = Field(default=None, json_schema_extra={"writeOnly": True})


class PasswordChangeRequest(ApiModel):
    currentPassword: str | None = Field(default=None, json_schema_extra={"writeOnly": True})
    password: str | None = Field(default=None, json_schema_extra={"writeOnly": True})


class JsonObjectRequest(ApiModel):
    """Compatibility schema for existing object-shaped route payloads."""


class ErrorResponse(BaseModel):
    error: str
    code: str
    requestId: str


class HealthResponse(BaseModel):
    status: str = "healthy"
    version: str
    timestamp: datetime


class DependencyStatus(BaseModel):
    status: str
    message: str | None = None


class ReadinessResponse(HealthResponse):
    dependencies: dict[str, DependencyStatus]


class StatusValue(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"
    STALE = "stale"


class StatusIssue(BaseModel):
    component: str
    code: str
    message: str
    status: StatusValue


class ComponentStatus(BaseModel):
    status: StatusValue
    source_checked_at: datetime | None = None
    healthy: int = 0
    total: int = 0
    components: list[str] = Field(default_factory=list)
    issues: list[StatusIssue] = Field(default_factory=list)


class TrueNASStatus(BaseModel):
    status: StatusValue
    source_checked_at: datetime | None = None
    pool: str | None = None
    active_alerts: int | None = None
    issues: list[StatusIssue] = Field(default_factory=list)


class StatusSummaryResponse(BaseModel):
    overall: StatusValue
    network: ComponentStatus
    proxmox: ComponentStatus
    truenas: TrueNASStatus
    docker: ComponentStatus
    checked_at: datetime
    stale: bool


REQUEST_MODELS: dict[str, type[BaseModel]] = {
    "setup": CredentialsRequest,
    "login": CredentialsRequest,
    "account-password": PasswordChangeRequest,
}


def request_schema(route_name: str) -> dict[str, Any]:
    model = REQUEST_MODELS.get(route_name, JsonObjectRequest)
    return model.model_json_schema()
