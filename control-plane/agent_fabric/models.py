import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RunState(enum.StrEnum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    LOST = "LOST"


TERMINAL_STATES = {
    RunState.SUCCEEDED,
    RunState.FAILED,
    RunState.CANCELLED,
    RunState.TIMED_OUT,
    RunState.LOST,
}


class AttemptState(enum.StrEnum):
    OFFERED = "OFFERED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    LOST = "LOST"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    api_key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    weight: Mapped[int] = mapped_column(default=1)
    max_queued: Mapped[int] = mapped_column(default=1000)
    max_running: Mapped[int] = mapped_column(default=20)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key"),
        Index("ix_runs_schedulable", "state", "priority", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[RunState] = mapped_column(
        Enum(RunState, native_enum=False), default=RunState.QUEUED
    )
    priority: Mapped[int] = mapped_column(default=5)
    retry_safe: Mapped[bool] = mapped_column(Boolean, default=False)
    max_attempts: Mapped[int] = mapped_column(default=1)
    attempt_count: Mapped[int] = mapped_column(default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    attempts: Mapped[list["Attempt"]] = relationship(back_populates="run", lazy="selectin")


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    protocol_version: Mapped[str] = mapped_column(String(32))
    worker_version: Mapped[str] = mapped_column(String(64))
    cpu_millis: Mapped[int] = mapped_column(BigInteger)
    memory_mb: Mapped[int] = mapped_column(BigInteger)
    pids: Mapped[int] = mapped_column(Integer)
    reserved_cpu_millis: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_memory_mb: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_pids: Mapped[int] = mapped_column(Integer, default=0)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    sandbox_backends: Mapped[list[str]] = mapped_column(JSON, default=list)
    draining: Mapped[bool] = mapped_column(Boolean, default=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("run_id", "number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"), index=True)
    number: Mapped[int]
    state: Mapped[AttemptState] = mapped_column(
        Enum(AttemptState, native_enum=False), default=AttemptState.OFFERED
    )
    lease_token_hash: Mapped[str] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleanup_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="attempts")


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_unpublished", "published_at", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic: Mapped[str] = mapped_column(String(80))
    aggregate_id: Mapped[str] = mapped_column(String(120), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEventIndex(Base):
    __tablename__ = "run_event_indexes"
    __table_args__ = (UniqueConstraint("attempt_id", "sequence"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id"), index=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    stream: Mapped[str] = mapped_column(String(20))
    object_key: Mapped[str] = mapped_column(String(500))
    byte_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
