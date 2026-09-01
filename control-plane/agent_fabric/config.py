from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agent_fabric:agent_fabric@localhost:5432/agent_fabric"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "agent_fabric"
    minio_secret_key: str = "agent_fabric_dev_only"
    minio_bucket: str = "run-logs"
    minio_secure: bool = False
    api_key: str = "af_dev_key"
    api_key_project: str = "local"
    # Admission limits applied to the seeded development project on every startup.
    project_max_queued: int = Field(default=1000, ge=1)
    project_max_running: int = Field(default=20, ge=1)
    grpc_bind: str = "0.0.0.0:50051"
    # Scheduler and gRPC gateway expose Prometheus metrics here when set (the API uses /metrics).
    metrics_port: int | None = None
    otel_exporter_otlp_endpoint: str | None = None
    heartbeat_interval_seconds: int = Field(default=5, ge=1)
    unhealthy_after_seconds: int = Field(default=15, ge=2)
    acknowledgement_deadline_seconds: int = Field(default=10, ge=1)
    max_log_bytes: int = 10 * 1024 * 1024
    scheduler_poll_seconds: float = 0.5
    auto_create_schema: bool = True

    profile_images: dict[str, str] = {
        "python": "python:3.12-slim",
        "node": "node:22-slim",
        "go": "golang:1.24-bookworm",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
