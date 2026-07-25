import asyncio
import base64
import binascii
import hashlib
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import timedelta
from typing import Any, BinaryIO, Protocol, cast

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from daily_insights_api.modules.assets.object_store import (
    ObjectMetadata,
    ObjectRef,
)


class _StreamingBody(Protocol):
    def read(self, amount: int | None = None) -> bytes: ...

    def close(self) -> None: ...


class S3Client(Protocol):
    def head_object(self, **kwargs: object) -> dict[str, Any]: ...

    def get_object(self, **kwargs: object) -> dict[str, Any]: ...

    def put_object(self, **kwargs: object) -> dict[str, Any]: ...

    def delete_object(self, **kwargs: object) -> dict[str, Any]: ...

    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: Mapping[str, object],
        ExpiresIn: int,
    ) -> str: ...


ClientFactory = Callable[..., BaseClient]


def _content_sha256(response: Mapping[str, Any]) -> str | None:
    custom_metadata = response.get("Metadata")
    if isinstance(custom_metadata, Mapping):
        value = custom_metadata.get("sha256")
        if isinstance(value, str):
            normalized = value.lower()
            if len(normalized) == 64 and all(
                character in "0123456789abcdef" for character in normalized
            ):
                return normalized

    checksum = response.get("ChecksumSHA256")
    if isinstance(checksum, str):
        try:
            decoded = base64.b64decode(checksum, validate=True)
        except (binascii.Error, ValueError):
            return None
        if len(decoded) == 32:
            return decoded.hex()
    return None


def _is_not_found(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", "")).lower()
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "nosuchkey", "notfound"} or status == 404


def _is_precondition_failed(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", "")).lower()
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"412", "preconditionfailed"} or status == 412


class R2ObjectStore:
    """S3-compatible Cloudflare R2 adapter with a non-blocking async boundary.

    Conditional copies spool into a seekable file before boto3 request
    preparation. Memory is bounded by ``copy_spool_memory_bytes``; larger
    objects transparently spill to the host's temporary disk and are always
    closed after the request.
    """

    def __init__(
        self,
        client: S3Client,
        *,
        read_chunk_size: int = 1024 * 1024,
        copy_spool_memory_bytes: int = 8 * 1024 * 1024,
        max_copy_size_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        if read_chunk_size <= 0 or copy_spool_memory_bytes <= 0 or max_copy_size_bytes <= 0:
            raise ValueError("object-store size limits must be positive")
        self._client = client
        self._read_chunk_size = read_chunk_size
        self._copy_spool_memory_bytes = copy_spool_memory_bytes
        self._max_copy_size_bytes = max_copy_size_bytes

    @classmethod
    def from_credentials(
        cls,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        region_name: str = "auto",
        client_factory: ClientFactory = boto3.client,
    ) -> "R2ObjectStore":
        client = client_factory(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region_name,
        )
        return cls(cast(S3Client, client))

    async def head(self, ref: ObjectRef) -> ObjectMetadata | None:
        try:
            response = await asyncio.to_thread(
                self._client.head_object,
                Bucket=ref.bucket,
                Key=ref.key,
                ChecksumMode="ENABLED",
            )
        except ClientError as error:
            if _is_not_found(error):
                return None
            raise
        return ObjectMetadata(
            ref=ref,
            size_bytes=int(response["ContentLength"]),
            mime_type=str(response.get("ContentType") or "application/octet-stream"),
            sha256=_content_sha256(response),
            etag=str(response["ETag"]) if response.get("ETag") is not None else None,
        )

    def _copy_if_absent(self, source: ObjectRef, target: ObjectRef, sha256: str) -> bool:
        response = self._client.get_object(
            Bucket=source.bucket,
            Key=source.key,
            ChecksumMode="ENABLED",
        )
        body = cast(_StreamingBody, response["Body"])
        try:
            content_type = str(response.get("ContentType") or "application/octet-stream")
            content_length_value = response.get("ContentLength")
            if content_length_value is None:
                raise ValueError("source ContentLength is required for conditional copy")
            content_length = int(content_length_value)
            if content_length <= 0 or content_length > self._max_copy_size_bytes:
                raise ValueError("source object size is outside configured copy limits")
            metadata = dict(response.get("Metadata") or {})
            metadata["sha256"] = sha256
            with tempfile.SpooledTemporaryFile(
                max_size=self._copy_spool_memory_bytes,
                mode="w+b",
            ) as spool:
                copied = 0
                copied_digest = hashlib.sha256()
                while True:
                    chunk = body.read(self._read_chunk_size)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > content_length or copied > self._max_copy_size_bytes:
                        raise ValueError("source body exceeds declared or configured copy size")
                    spool.write(chunk)
                    copied_digest.update(chunk)
                if copied != content_length:
                    raise ValueError("source body does not match declared ContentLength")
                computed_sha256 = copied_digest.hexdigest()
                if computed_sha256 != sha256:
                    raise ValueError("source body checksum changed before conditional copy")
                spool.seek(0)
                try:
                    self._client.put_object(
                        Bucket=target.bucket,
                        Key=target.key,
                        Body=spool,
                        ContentType=content_type,
                        Metadata={**metadata, "sha256": computed_sha256},
                        ContentLength=content_length,
                        IfNoneMatch="*",
                    )
                except ClientError as error:
                    if _is_precondition_failed(error):
                        return False
                    raise
        finally:
            body.close()
        return True

    async def copy_if_absent(
        self,
        source: ObjectRef,
        target: ObjectRef,
        *,
        sha256: str,
    ) -> bool:
        return await asyncio.to_thread(self._copy_if_absent, source, target, sha256)

    def _put_if_absent(
        self,
        target: ObjectRef,
        content: BinaryIO,
        *,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> bool:
        if size_bytes <= 0 or size_bytes > self._max_copy_size_bytes:
            raise ValueError("upload size is outside configured limits")
        content.seek(0)
        try:
            self._client.put_object(
                Bucket=target.bucket,
                Key=target.key,
                Body=content,
                ContentType=mime_type,
                Metadata={"sha256": sha256},
                ContentLength=size_bytes,
                IfNoneMatch="*",
            )
        except ClientError as error:
            if _is_precondition_failed(error):
                return False
            raise
        return True

    async def put_if_absent(
        self,
        target: ObjectRef,
        content: BinaryIO,
        *,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._put_if_absent,
            target,
            content,
            size_bytes=size_bytes,
            mime_type=mime_type,
            sha256=sha256,
        )

    def _overwrite(
        self,
        target: ObjectRef,
        content: BinaryIO,
        *,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> None:
        if size_bytes <= 0 or size_bytes > self._max_copy_size_bytes:
            raise ValueError("upload size is outside configured limits")
        content.seek(0)
        self._client.put_object(
            Bucket=target.bucket,
            Key=target.key,
            Body=content,
            ContentType=mime_type,
            Metadata={"sha256": sha256},
            ContentLength=size_bytes,
        )

    async def overwrite(
        self,
        target: ObjectRef,
        content: BinaryIO,
        *,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> None:
        await asyncio.to_thread(
            self._overwrite,
            target,
            content,
            size_bytes=size_bytes,
            mime_type=mime_type,
            sha256=sha256,
        )

    async def delete(self, target: ObjectRef) -> None:
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=target.bucket,
            Key=target.key,
        )

    def read(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        async def chunks() -> AsyncIterator[bytes]:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=ref.bucket,
                Key=ref.key,
                ChecksumMode="ENABLED",
            )
            body = cast(_StreamingBody, response["Body"])
            try:
                while True:
                    chunk = await asyncio.to_thread(body.read, self._read_chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await asyncio.to_thread(body.close)

        return chunks()

    async def presign_get(self, ref: ObjectRef, expires_in: timedelta) -> str:
        expires_in_seconds = int(expires_in.total_seconds())
        if expires_in_seconds <= 0:
            raise ValueError("signed URL lifetime must be positive")
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": ref.bucket, "Key": ref.key},
            ExpiresIn=expires_in_seconds,
        )
