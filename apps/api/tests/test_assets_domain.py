import hashlib
import io
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import IO, Any, Protocol, cast

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from daily_insights_api.core.enums import AssetKind, AssetStatus
from daily_insights_api.modules.assets.api import (
    AssetForSigning,
    canonical_podcast_audio_key,
)
from daily_insights_api.modules.assets.object_store import ObjectMetadata, ObjectRef
from daily_insights_api.modules.assets.r2.store import R2ObjectStore, S3Client
from daily_insights_api.modules.assets.service import (
    AssetNotSignableError,
    sign_asset_download,
)
from daily_insights_api.modules.podcasts.api import (
    AudioVariantConflictError,
    PodcastAudioVariant,
    prepare_audio_replacement,
    resolve_audio_variant,
)


class SigningStore:
    def __init__(self, metadata: ObjectMetadata | None, body: bytes = b"podcast") -> None:
        self.metadata = metadata
        self.body = body
        self.signed = 0

    async def head(self, ref: ObjectRef) -> ObjectMetadata | None:
        del ref
        return self.metadata

    async def copy_if_absent(
        self,
        source: ObjectRef,
        target: ObjectRef,
        *,
        sha256: str,
    ) -> bool:
        del source, target, sha256
        raise AssertionError("signing must not copy")

    def read(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        del ref

        async def chunks() -> AsyncIterator[bytes]:
            yield self.body

        return chunks()

    async def presign_get(self, ref: ObjectRef, expires_in: timedelta) -> str:
        self.signed += 1
        return f"https://private.invalid/{ref.key}?ttl={int(expires_in.total_seconds())}"


class StubStreamingBody:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)
        self.closed = False

    def read(self, amount: int | None = None) -> bytes:
        return self._body.read(-1 if amount is None else amount)

    def close(self) -> None:
        self.closed = True


class ObservedSpool(Protocol):
    closed: bool
    _rolled: bool


class StubS3Client:
    def __init__(self) -> None:
        self.body_bytes = b"podcast"
        self.bodies: list[StubStreamingBody] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.missing = False
        self.precondition_failed = False
        self.content_length = 7
        self.put_body_rolled: bool | None = None
        self.put_body: ObservedSpool | None = None

    def head_object(self, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("head", kwargs))
        if self.missing:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {
                        "RequestId": "request-id",
                        "HostId": "host-id",
                        "HTTPStatusCode": 404,
                        "HTTPHeaders": {},
                        "RetryAttempts": 0,
                    },
                },
                "HeadObject",
            )
        return {
            "ContentLength": 7,
            "ContentType": "audio/mpeg",
            "Metadata": {"sha256": hashlib.sha256(b"podcast").hexdigest()},
            "ETag": '"multipart-etag-2"',
        }

    def get_object(self, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        body = StubStreamingBody(self.body_bytes)
        self.bodies.append(body)
        return {
            "Body": body,
            "ContentLength": self.content_length,
            "ContentType": "audio/mpeg",
            "Metadata": {"legacy": "true"},
        }

    def put_object(self, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("put", kwargs))
        self.put_body = cast(ObservedSpool, kwargs["Body"])
        self.put_body_rolled = bool(self.put_body._rolled)
        if self.precondition_failed:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed", "Message": "exists"},
                    "ResponseMetadata": {
                        "RequestId": "request-id",
                        "HostId": "host-id",
                        "HTTPStatusCode": 412,
                        "HTTPHeaders": {},
                        "RetryAttempts": 0,
                    },
                },
                "PutObject",
            )
        return {}

    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: dict[str, object],
        ExpiresIn: int,
    ) -> str:
        self.calls.append(
            (
                "presign",
                {"method": client_method, "params": Params, "expires": ExpiresIn},
            )
        )
        return "https://signed.invalid/private"


def _asset(*, status: AssetStatus = AssetStatus.ACTIVE) -> AssetForSigning:
    body = b"podcast"
    return AssetForSigning(
        id=uuid.uuid4(),
        ref=ObjectRef(bucket="private", key="podcast.mp3"),
        kind=AssetKind.AUDIO,
        status=status,
        mime_type="audio/mpeg",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )


def test_canonical_key_is_backend_controlled_and_locale_aware() -> None:
    asset_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert canonical_podcast_audio_key(
        trading_date=date(2026, 7, 24),
        locale="zh-TW",
        asset_id=asset_id,
        mime_type="audio/mpeg",
    ) == (
        "podcasts/2026-07-24/audio/zh-TW/"
        "podcast-2026-07-24-zh-TW-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.mp3"
    )


def test_locale_resolution_is_exact_then_only_zh_tw() -> None:
    zh_tw = PodcastAudioVariant(asset_id=uuid.uuid4(), locale="zh-TW", version=1)
    zh_cn = PodcastAudioVariant(asset_id=uuid.uuid4(), locale="zh-CN", version=1)

    exact = resolve_audio_variant((zh_tw, zh_cn), "zh-CN")
    fallback = resolve_audio_variant((zh_tw,), "en")

    assert exact.resolved_locale == "zh-CN"
    assert exact.variant.asset_id == zh_cn.asset_id
    assert fallback.requested_locale == "en"
    assert fallback.resolved_locale == "zh-TW"


def test_replacement_requires_expected_current_version() -> None:
    current = PodcastAudioVariant(asset_id=uuid.uuid4(), locale="en", version=3)
    replacement_id = uuid.uuid4()
    replacement = prepare_audio_replacement(
        current=current,
        expected_current_version=3,
        new_asset_id=replacement_id,
    )
    assert replacement.next_version == 4
    assert replacement.new_asset_id == replacement_id

    with pytest.raises(AudioVariantConflictError):
        prepare_audio_replacement(
            current=current,
            expected_current_version=2,
            new_asset_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_verified_active_asset_can_receive_short_lived_url() -> None:
    asset = _asset()
    store = SigningStore(
        ObjectMetadata(
            ref=asset.ref,
            size_bytes=asset.size_bytes,
            mime_type=asset.mime_type,
            sha256=asset.sha256,
        )
    )
    signed = await sign_asset_download(store, asset, expires_in=timedelta(minutes=5))
    assert signed.asset_id == asset.id
    assert signed.expires_in_seconds == 300
    assert store.signed == 1


@pytest.mark.asyncio
async def test_signing_hashes_current_bytes_when_head_has_no_explicit_checksum() -> None:
    asset = _asset()
    store = SigningStore(
        ObjectMetadata(
            ref=asset.ref,
            size_bytes=asset.size_bytes,
            mime_type=asset.mime_type,
            sha256=None,
            etag='"multipart-etag-2"',
        ),
        body=b"badcast",
    )
    with pytest.raises(AssetNotSignableError, match="checksum"):
        await sign_asset_download(store, asset, expires_in=timedelta(minutes=5))
    assert store.signed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        AssetStatus.PENDING_VERIFICATION,
        AssetStatus.QUARANTINED,
        AssetStatus.MISSING,
        AssetStatus.ARCHIVED,
        AssetStatus.DELETED,
    ],
)
async def test_non_signable_asset_never_reaches_signer(status: AssetStatus) -> None:
    asset = _asset(status=status)
    store = SigningStore(None)
    with pytest.raises(AssetNotSignableError):
        await sign_asset_download(store, asset, expires_in=timedelta(minutes=5))
    assert store.signed == 0


@pytest.mark.asyncio
async def test_r2_adapter_maps_s3_calls_without_treating_etag_as_checksum() -> None:
    client = StubS3Client()
    store = R2ObjectStore(cast(S3Client, client), read_chunk_size=3)
    source = ObjectRef(bucket="private", key="source.mp3")
    target = ObjectRef(bucket="private", key="target.mp3")

    metadata = await store.head(source)
    assert metadata is not None
    assert metadata.sha256 == hashlib.sha256(b"podcast").hexdigest()
    assert metadata.etag == '"multipart-etag-2"'
    assert b"".join([chunk async for chunk in store.read(source)]) == b"podcast"
    assert client.bodies[-1].closed

    digest = hashlib.sha256(b"podcast").hexdigest()
    assert await store.copy_if_absent(source, target, sha256=digest)
    signed = await store.presign_get(target, timedelta(seconds=90))
    assert signed == "https://signed.invalid/private"
    put_call = next(arguments for name, arguments in client.calls if name == "put")
    assert put_call["Bucket"] == "private"
    assert put_call["Key"] == "target.mp3"
    assert put_call["IfNoneMatch"] == "*"
    assert put_call["ContentLength"] == 7
    assert put_call["Metadata"] == {"legacy": "true", "sha256": digest}
    assert client.put_body is not None and client.put_body.closed


@pytest.mark.asyncio
async def test_r2_atomic_copy_reports_concurrent_destination_without_overwrite() -> None:
    client = StubS3Client()
    client.precondition_failed = True
    store = R2ObjectStore(cast(S3Client, client))
    created = await store.copy_if_absent(
        ObjectRef(bucket="private", key="source.mp3"),
        ObjectRef(bucket="private", key="target.mp3"),
        sha256=hashlib.sha256(b"podcast").hexdigest(),
    )
    assert not created
    assert client.bodies[-1].closed
    assert client.put_body is not None and client.put_body.closed


@pytest.mark.asyncio
async def test_r2_atomic_copy_spills_to_disk_above_memory_threshold() -> None:
    client = StubS3Client()
    store = R2ObjectStore(
        cast(S3Client, client),
        copy_spool_memory_bytes=1,
    )
    assert await store.copy_if_absent(
        ObjectRef(bucket="private", key="source.mp3"),
        ObjectRef(bucket="private", key="target.mp3"),
        sha256=hashlib.sha256(b"podcast").hexdigest(),
    )
    assert client.put_body_rolled is True
    assert client.bodies[-1].closed
    assert client.put_body is not None and client.put_body.closed


@pytest.mark.asyncio
async def test_r2_atomic_copy_rejects_size_mismatch_and_closes_source() -> None:
    client = StubS3Client()
    client.content_length = 8
    store = R2ObjectStore(cast(S3Client, client))
    with pytest.raises(ValueError, match="ContentLength"):
        await store.copy_if_absent(
            ObjectRef(bucket="private", key="source.mp3"),
            ObjectRef(bucket="private", key="target.mp3"),
            sha256=hashlib.sha256(b"podcast").hexdigest(),
        )
    assert client.bodies[-1].closed
    assert not any(name == "put" for name, _ in client.calls)


@pytest.mark.asyncio
async def test_r2_atomic_copy_rehashes_spooled_source_before_put() -> None:
    client = StubS3Client()
    client.body_bytes = b"badcast"
    store = R2ObjectStore(cast(S3Client, client))
    with pytest.raises(ValueError, match="checksum changed"):
        await store.copy_if_absent(
            ObjectRef(bucket="private", key="source.mp3"),
            ObjectRef(bucket="private", key="target.mp3"),
            sha256=hashlib.sha256(b"podcast").hexdigest(),
        )
    assert client.bodies[-1].closed
    assert not any(name == "put" for name, _ in client.calls)


class StopBeforeTransport(RuntimeError):
    pass


class BotocorePreparationClient(StubS3Client):
    def __init__(self) -> None:
        super().__init__()
        self.real_client = boto3.client(
            "s3",
            endpoint_url="http://127.0.0.1:9",
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
            region_name="auto",
            config=Config(retries={"max_attempts": 0}),
        )
        self.real_client.meta.events.register(
            "before-send.s3.PutObject",
            self._stop_before_transport,
        )

    @staticmethod
    def _stop_before_transport(**kwargs: object) -> None:
        del kwargs
        raise StopBeforeTransport

    def put_object(self, **kwargs: object) -> dict[str, Any]:
        self.put_body = cast(ObservedSpool, kwargs["Body"])
        self.put_body_rolled = bool(self.put_body._rolled)
        return cast(
            dict[str, Any],
            self.real_client.put_object(
                Bucket=cast(str, kwargs["Bucket"]),
                Key=cast(str, kwargs["Key"]),
                Body=cast(IO[Any], kwargs["Body"]),
                ContentType=cast(str, kwargs["ContentType"]),
                Metadata=cast(dict[str, str], kwargs["Metadata"]),
                ContentLength=cast(int, kwargs["ContentLength"]),
                IfNoneMatch=cast(str, kwargs["IfNoneMatch"]),
            ),
        )


@pytest.mark.asyncio
async def test_real_botocore_prepares_seekable_conditional_put_before_transport() -> None:
    client = BotocorePreparationClient()
    store = R2ObjectStore(cast(S3Client, client))
    with pytest.raises(StopBeforeTransport):
        await store.copy_if_absent(
            ObjectRef(bucket="private", key="source.mp3"),
            ObjectRef(bucket="private", key="target.mp3"),
            sha256=hashlib.sha256(b"podcast").hexdigest(),
        )
    assert client.bodies[-1].closed
    assert client.put_body is not None and client.put_body.closed


@pytest.mark.asyncio
async def test_r2_adapter_returns_none_only_for_object_not_found() -> None:
    client = StubS3Client()
    client.missing = True
    store = R2ObjectStore(cast(S3Client, client))
    assert await store.head(ObjectRef(bucket="private", key="missing.mp3")) is None
