from enum import StrEnum


class AssetKind(StrEnum):
    AUDIO = "audio"
    IMAGE = "image"
    DOWNLOADABLE = "downloadable"
    PDF = "pdf"


class AssetStatus(StrEnum):
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    MISSING = "missing"
    ARCHIVED = "archived"
    DELETED = "deleted"


class GenerationStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    PARTIAL = "partial"
    ERROR = "error"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class SystemRole(StrEnum):
    ADMIN = "admin"
    ASSET_MANAGER = "asset_manager"
    ORG_MEMBER = "org_member"


class UserStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
