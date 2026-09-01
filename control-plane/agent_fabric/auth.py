import hashlib
import hmac
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session
from .models import Project

bearer = HTTPBearer(auto_error=False)


def hash_api_key(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


async def seed_development_project(session: AsyncSession) -> None:
    settings = get_settings()
    digest = hash_api_key(settings.api_key)
    project = await session.scalar(select(Project).where(Project.api_key_hash == digest))
    if project is None:
        session.add(
            Project(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"agent-fabric:{settings.api_key_project}"),
                slug=settings.api_key_project,
                api_key_hash=digest,
                max_queued=settings.project_max_queued,
                max_running=settings.project_max_running,
            )
        )
    else:
        project.max_queued = settings.project_max_queued
        project.max_running = settings.project_max_running
    await session.commit()


async def authenticated_project(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Project:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing API key")
    supplied = hash_api_key(credentials.credentials)
    projects = (await session.scalars(select(Project))).all()
    for project in projects:
        if hmac.compare_digest(project.api_key_hash, supplied):
            return project
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
