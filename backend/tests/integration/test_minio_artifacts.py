"""Real MinIO/S3 primitives used by immutable artifact commit."""
from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import httpx
import pytest

from app.object_store import (
    copy_object,
    create_presigned_upload,
    delete_object,
    get_object,
    head_object,
    list_objects,
)


RUN_MINIO = os.environ.get("CAD_AGENT_TEST_OBJECT_STORE") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_MINIO,
    reason="CAD_AGENT_TEST_OBJECT_STORE=1 is required for real MinIO tests",
)


@pytest.mark.asyncio
async def test_presigned_put_head_read_copy_list_and_delete_round_trip():
    payload = b"ISO-10303-21;\nREAL-MINIO-ARTIFACT\nEND-ISO-10303-21;\n"
    digest = hashlib.sha256(payload).hexdigest()
    source_key = f"_integration/artifacts/{uuid4()}/source.step"
    destination_key = f"_integration/artifacts/{uuid4()}/copy.step"
    upload = create_presigned_upload(
        source_key,
        content_type="application/step",
        sha256=digest,
        expires_in=120,
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.put(
                upload["url"],
                content=payload,
                headers=upload["headers"],
            )
        assert response.status_code in {200, 204}, response.text

        metadata = await head_object(source_key)
        assert metadata["size_bytes"] == len(payload)
        assert metadata["metadata"]["sha256"] == digest
        assert await get_object(source_key) == payload

        await copy_object(source_key, destination_key)
        assert await get_object(destination_key) == payload
        keys = {item["key"] for item in await list_objects("_integration/artifacts/")}
        assert source_key in keys
        assert destination_key in keys
    finally:
        await delete_object(source_key)
        await delete_object(destination_key)
