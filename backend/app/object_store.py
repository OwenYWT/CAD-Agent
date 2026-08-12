"""S3-compatible byte-store boundary used by the durable control plane."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
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


async def put_file(key: str, path: str | Path, *, content_type: str) -> dict:
    """Stream one local file into object storage and verify the stored bytes."""
    source = Path(path)

    def _upload() -> tuple[int, str]:
        digest = hashlib.sha256()
        size_bytes = 0
        with source.open("rb") as handle:
            while chunk := handle.read(8 * 1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
        checksum = digest.hexdigest()
        with source.open("rb") as handle:
            get_object_store_client().upload_fileobj(
                handle,
                settings.object_store_bucket,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {"sha256": checksum},
                },
            )
        return size_bytes, checksum

    expected_size, expected_sha = await asyncio.to_thread(_upload)
    stored = await sha256_object(key)
    if (
        stored["size_bytes"] != expected_size
        or stored["sha256"] != expected_sha
    ):
        await delete_object(key)
        raise RuntimeError("object-store file upload checksum mismatch")
    return {
        "key": key,
        "size_bytes": expected_size,
        "sha256": expected_sha,
    }


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


async def download_object(key: str, destination: str | Path) -> dict:
    """Stream one immutable object to disk and return measured integrity facts."""
    target = Path(destination)

    def _download() -> dict:
        response = get_object_store_client().get_object(
            Bucket=settings.object_store_bucket,
            Key=key,
        )
        body = response["Body"]
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with target.open("wb") as handle:
                while chunk := body.read(8 * 1024 * 1024):
                    handle.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
        finally:
            body.close()
        return {
            "key": key,
            "size_bytes": size_bytes,
            "sha256": digest.hexdigest(),
        }

    return await asyncio.to_thread(_download)


async def sha256_object(key: str, *, chunk_size: int = 8 * 1024 * 1024) -> dict:
    """Stream an object's bytes through SHA-256 without loading it into RAM."""
    def _hash() -> dict:
        response = get_object_store_client().get_object(
            Bucket=settings.object_store_bucket,
            Key=key,
        )
        body = response["Body"]
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                size_bytes += len(chunk)
        finally:
            body.close()
        return {
            "key": key,
            "size_bytes": size_bytes,
            "sha256": digest.hexdigest(),
        }

    return await asyncio.to_thread(_hash)


async def delete_object(key: str) -> None:
    await _call(
        get_object_store_client().delete_object,
        Bucket=settings.object_store_bucket,
        Key=key,
    )


async def head_object(key: str) -> dict:
    response = await _call(
        get_object_store_client().head_object,
        Bucket=settings.object_store_bucket,
        Key=key,
    )
    return {
        "key": key,
        "size_bytes": int(response["ContentLength"]),
        "content_type": response.get("ContentType"),
        "metadata": dict(response.get("Metadata") or {}),
        "etag": str(response.get("ETag") or "").strip('"'),
        "last_modified": response.get("LastModified"),
    }


async def copy_object(source_key: str, destination_key: str) -> dict:
    await _call(
        get_object_store_client().copy_object,
        Bucket=settings.object_store_bucket,
        Key=destination_key,
        CopySource={
            "Bucket": settings.object_store_bucket,
            "Key": source_key,
        },
        MetadataDirective="COPY",
    )
    return await head_object(destination_key)


async def list_objects(prefix: str) -> list[dict]:
    def _list() -> list[dict]:
        paginator = get_object_store_client().get_paginator("list_objects_v2")
        items: list[dict] = []
        for page in paginator.paginate(
            Bucket=settings.object_store_bucket,
            Prefix=prefix,
        ):
            for item in page.get("Contents") or []:
                last_modified = item.get("LastModified")
                items.append(
                    {
                        "key": item["Key"],
                        "size_bytes": int(item["Size"]),
                        "etag": str(item.get("ETag") or "").strip('"'),
                        "last_modified": last_modified,
                    }
                )
        return items

    return await asyncio.to_thread(_list)


def create_presigned_upload(
    key: str,
    *,
    content_type: str,
    sha256: str,
    expires_in: int | None = None,
) -> dict:
    ttl = expires_in or settings.object_store_presign_ttl_s
    params = {
        "Bucket": settings.object_store_bucket,
        "Key": key,
        "ContentType": content_type,
        "Metadata": {"sha256": sha256},
    }
    return {
        "url": get_object_store_client().generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=ttl,
            HttpMethod="PUT",
        ),
        "headers": {
            "Content-Type": content_type,
            "x-amz-meta-sha256": sha256,
        },
        "expires_in": ttl,
    }


def reset_object_store_client() -> None:
    """Drop the cached client after a settings change or process shutdown."""
    global _client
    _client = None


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
