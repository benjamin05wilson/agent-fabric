import asyncio
from io import BytesIO

from minio import Minio

from .config import get_settings


class LogStore:
    def __init__(self) -> None:
        settings = get_settings()
        self.bucket = settings.minio_bucket
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    async def ensure_bucket(self) -> None:
        exists = await asyncio.to_thread(self.client.bucket_exists, self.bucket)
        if not exists:
            await asyncio.to_thread(self.client.make_bucket, self.bucket)

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            self.bucket,
            key,
            BytesIO(data),
            len(data),
            content_type="application/octet-stream",
        )

    async def get(self, key: str) -> bytes:
        response = await asyncio.to_thread(self.client.get_object, self.bucket, key)
        try:
            return await asyncio.to_thread(response.read)
        finally:
            response.close()
            response.release_conn()


log_store = LogStore()
