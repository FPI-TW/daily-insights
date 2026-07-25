from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str


@dataclass(frozen=True)
class ObjectMetadata:
    ref: ObjectRef
    size_bytes: int
    mime_type: str
    sha256: str | None = None
    etag: str | None = None


class ObjectStore(Protocol):
    """Private object-store boundary.

    Migration workflows never call deletion, so legacy source objects remain
    protected. ETag is transport metadata and is never a content digest.
    """

    async def head(self, ref: ObjectRef) -> ObjectMetadata | None: ...

    async def copy_if_absent(
        self,
        source: ObjectRef,
        target: ObjectRef,
        *,
        sha256: str,
    ) -> bool:
        """Atomically create target and return true; return false when it exists."""
        ...

    async def put_if_absent(
        self,
        target: ObjectRef,
        content: BinaryIO,
        *,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> bool:
        """Atomically upload a verified stream without replacing an existing key."""
        ...

    async def overwrite(
        self,
        target: ObjectRef,
        content: BinaryIO,
        *,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> None:
        """Upload a verified stream, replacing the target object when it exists."""
        ...

    async def delete(self, target: ObjectRef) -> None:
        """Delete an application-owned object superseded by a canonical upload."""
        ...

    def read(self, ref: ObjectRef) -> AsyncIterator[bytes]: ...

    async def presign_get(self, ref: ObjectRef, expires_in: timedelta) -> str: ...
