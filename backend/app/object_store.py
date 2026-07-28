"""S3-compatible byte-store boundary used by the durable control plane."""
from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from functools import partial

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from app.config import settings


_client: BaseClient | None = None


def get_object_store_client() -> BaseClient:
    global _client
    if _client is None:
        if not settings.object_store_endpoint_url:
            raise RuntimeError("OBJECT_STORE_ENDPOINT_URL is not configured")
        _client = boto3.client(
            "s3",
            endpoint_url=settings.object_store_endpoint_url,
            aws_access_key_id=settings.object_store_access_key,
            aws_secret_access_key=settings.object_store_secret_key,
            region_name=settings.object_store_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=settings.dependency_readiness_timeout_s,
                read_timeout=settings.dependency_readiness_timeout_s,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
    return _client


async def _call(method, **kwargs):
    return await asyncio.to_thread(partial(method, **kwargs))


async def object_store_readiness() -> dict:
    started = time.perf_counter()
    client = get_object_store_client()
    await asyncio.wait_for(
        _call(client.head_bucket, Bucket=settings.object_store_bucket),
        timeout=settings.dependency_readiness_timeout_s,
    )
    return {
        "status": "ready",
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


async def put_object(key: str, payload: bytes, *, content_type: str) -> dict:
    checksum = hashlib.sha256(payload).hexdigest()
    await _call(
        get_object_store_client().put_object,
        Bucket=settings.object_store_bucket,
        Key=key,
        Body=payload,
        ContentType=content_type,
        Metadata={"sha256": checksum},
    )
    return {"key": key, "size_bytes": len(payload), "sha256": checksum}


async def get_object(key: str) -> bytes:
    response = await _call(
        get_object_store_client().get_object,
        Bucket=settings.object_store_bucket,
        Key=key,
    )
    body = response["Body"]
    try:
        return await asyncio.to_thread(body.read)
    finally:
        body.close()


async def delete_object(key: str) -> None:
    await _call(
        get_object_store_client().delete_object,
        Bucket=settings.object_store_bucket,
        Key=key,
    )


async def object_store_round_trip() -> dict:
    """Put, read, verify, and delete one isolated readiness object."""
    payload = b"cad-agent-object-store-round-trip-v1"
    key = f"_readiness/{uuid.uuid4()}"
    expected = hashlib.sha256(payload).hexdigest()
    uploaded = False
    try:
        evidence = await put_object(
            key,
            payload,
            content_type="application/octet-stream",
        )
        uploaded = True
        restored = await get_object(key)
        if restored != payload or evidence["sha256"] != expected:
            raise RuntimeError("object-store round-trip checksum mismatch")
        return {
            "status": "success",
            "key": key,
            "size_bytes": len(restored),
            "sha256": expected,
        }
    finally:
        if uploaded:
            await delete_object(key)


def presign_get(key: str) -> str:
    return get_object_store_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.object_store_bucket, "Key": key},
        ExpiresIn=settings.object_store_presign_ttl_s,
    )
