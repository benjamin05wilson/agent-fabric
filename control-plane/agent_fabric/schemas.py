import ipaddress
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import RunState


class RepositorySpec(BaseModel):
    url: str = Field(min_length=12, max_length=2048)
    ref: str = Field(default="HEAD", min_length=1, max_length=255)

    @field_validator("url")
    @classmethod
    def public_https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("repository must be a credential-free HTTPS URL")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return value
        if not address.is_global:
            raise ValueError("repository address must be public")
        return value

    @field_validator("ref")
    @classmethod
    def safe_ref(cls, value: str) -> str:
        if value.startswith("-") or ".." in value or any(c.isspace() for c in value):
            raise ValueError("invalid Git ref")
        return value


class ResourceSpec(BaseModel):
    cpu_millis: int = Field(default=1000, ge=100, le=8000)
    memory_mb: int = Field(default=512, ge=128, le=16384)
    pids: int = Field(default=128, ge=1, le=512)
    disk_mb: int = Field(default=1024, ge=128, le=20480)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    gpu: int = Field(default=0, ge=0, le=8)
    vram_mb: int = Field(default=0, ge=0, le=131072)

    @model_validator(mode="after")
    def gpu_and_vram_are_consistent(self) -> "ResourceSpec":
        if self.gpu == 0 and self.vram_mb:
            raise ValueError("vram_mb requires gpu > 0")
        if self.gpu > 0 and self.vram_mb == 0:
            raise ValueError("GPU jobs must request vram_mb")
        return self


class RetrySpec(BaseModel):
    safe_on_worker_loss: bool = False
    max_attempts: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def attempts_require_safety(self) -> "RetrySpec":
        if self.max_attempts > 1 and not self.safe_on_worker_loss:
            raise ValueError("max_attempts > 1 requires safe_on_worker_loss")
        return self


class RunCreate(BaseModel):
    repository: RepositorySpec
    argv: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        min_length=1, max_length=64
    )
    environment: dict[str, str] = Field(default_factory=dict)
    profile: Literal["python", "node", "go", "cuda"]
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    required_capabilities: list[str] = Field(default_factory=list, max_length=16)
    network: Literal["disabled", "open"] = "disabled"
    priority: int = Field(default=5, ge=0, le=9)
    retry: RetrySpec = Field(default_factory=RetrySpec)

    @field_validator("required_capabilities")
    @classmethod
    def bounded_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("required_capabilities must be unique")
        for capability in value:
            if (
                not capability
                or len(capability) > 64
                or not all(char.isalnum() or char in "-_" for char in capability)
            ):
                raise ValueError(f"invalid capability: {capability!r}")
        return value

    @model_validator(mode="after")
    def gpu_jobs_require_cuda(self) -> "RunCreate":
        if self.resources.gpu > 0 and "cuda" not in self.required_capabilities:
            raise ValueError("GPU jobs must require the cuda capability")
        return self

    @field_validator("environment")
    @classmethod
    def bounded_environment(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32 or sum(len(k) + len(v) for k, v in value.items()) > 8192:
            raise ValueError("environment exceeds limits")
        for key in value:
            if not key or len(key) > 128 or not key.replace("_", "A").isalnum():
                raise ValueError(f"invalid environment key: {key!r}")
        return value


class AttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: int
    worker_id: str
    state: str
    acknowledged_at: datetime | None
    finished_at: datetime | None


class RunResponse(BaseModel):
    id: str
    state: RunState
    specification: dict[str, object]
    attempts: list[AttemptResponse]
    result: dict[str, object] | None
    failure: dict[str, str] | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunAccepted(BaseModel):
    id: str
    state: RunState
    created_at: datetime


class LogRecord(BaseModel):
    cursor: int
    attempt_id: str
    sequence: int
    stream: str
    data: str
    created_at: datetime


class LogPage(BaseModel):
    records: list[LogRecord]
    next_cursor: int | None


class WorkerResponse(BaseModel):
    id: str
    version: str
    healthy: bool
    draining: bool
    capacity: dict[str, int]
    reserved: dict[str, int]
    capabilities: list[str]
    sandbox_backends: list[str]
    last_seen_at: datetime
