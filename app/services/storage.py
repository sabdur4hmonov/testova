"""Unified file storage — local filesystem or S3-compatible."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import BinaryIO

import aiofiles
import aiofiles.os

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _local_path(key: str) -> Path:
    return Path(settings.LOCAL_STORAGE_PATH) / key


async def save_file(data: bytes | BinaryIO, folder: str, filename: str | None = None) -> str:
    """Save a file and return its storage key."""
    ext = Path(filename).suffix if filename else ""
    key = f"{folder}/{uuid.uuid4()}{ext}"

    if settings.STORAGE_TYPE == "s3":
        return await _s3_save(data, key)
    return await _local_save(data, key)


async def _local_save(data: bytes | BinaryIO, key: str) -> str:
    path = _local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = data if isinstance(data, bytes) else data.read()
    async with aiofiles.open(path, "wb") as f:
        await f.write(content)
    logger.debug("saved_local", key=key)
    return key


async def _s3_save(data: bytes | BinaryIO, key: str) -> str:
    import boto3  # lazy import — only needed in s3 mode

    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        endpoint_url=settings.S3_ENDPOINT_URL,
        region_name=settings.S3_REGION,
    )
    content = data if isinstance(data, bytes) else data.read()
    s3.put_object(Bucket=settings.S3_BUCKET, Key=key, Body=content)
    logger.debug("saved_s3", key=key, bucket=settings.S3_BUCKET)
    return key


async def read_file(key: str) -> bytes:
    if settings.STORAGE_TYPE == "s3":
        return await _s3_read(key)
    return await _local_read(key)


async def _local_read(key: str) -> bytes:
    path = _local_path(key)
    async with aiofiles.open(path, "rb") as f:
        return await f.read()


async def _s3_read(key: str) -> bytes:
    import boto3

    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        endpoint_url=settings.S3_ENDPOINT_URL,
        region_name=settings.S3_REGION,
    )
    response = s3.get_object(Bucket=settings.S3_BUCKET, Key=key)
    return response["Body"].read()


def get_local_path(key: str) -> Path:
    """Return the filesystem Path for a storage key (local mode only)."""
    if settings.STORAGE_TYPE != "local":
        raise RuntimeError("get_local_path is only available in local storage mode")
    return _local_path(key)


async def delete_file(key: str) -> None:
    if settings.STORAGE_TYPE == "s3":
        import boto3
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            endpoint_url=settings.S3_ENDPOINT_URL,
        )
        s3.delete_object(Bucket=settings.S3_BUCKET, Key=key)
    else:
        path = _local_path(key)
        if path.exists():
            await aiofiles.os.remove(path)
