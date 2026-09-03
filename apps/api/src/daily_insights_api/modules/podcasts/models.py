import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PodcastEpisode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "podcast_episodes"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "status IN ('draft', 'published')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'draft' AND published_at IS NULL AND published_by_user_id IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL "
            "AND published_by_user_id IS NOT NULL)",
            name="publication_fields_consistent",
        ),
        Index("ix_podcast_episodes_status_date", "status", "trading_date"),
    )

    trading_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    cover_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="RESTRICT")
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PodcastEpisodeTranslation(TimestampMixin, Base):
    __tablename__ = "podcast_episode_translations"
    __table_args__ = (
        CheckConstraint("locale IN ('zh-hant', 'zh-hans', 'en')", name="locale_supported"),
        CheckConstraint("char_length(btrim(title)) > 0", name="title_nonempty"),
        CheckConstraint("char_length(btrim(summary)) > 0", name="summary_nonempty"),
    )

    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("podcast_episodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    locale: Mapped[str] = mapped_column(String(10), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)


class PodcastEpisodeAudioVariant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "podcast_episode_audio_variants"
    __table_args__ = (
        CheckConstraint("locale IN ('zh-hant', 'zh-hans', 'en')", name="locale_supported"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds > 0", name="duration_positive"
        ),
        UniqueConstraint(
            "episode_id",
            "locale",
            "version",
            name="uq_podcast_episode_audio_variant_version",
        ),
        Index(
            "uq_podcast_episode_audio_variant_active",
            "episode_id",
            "locale",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("ix_podcast_episode_audio_variants_asset", "asset_id"),
    )

    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("podcast_episodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    activated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Read from the file at upload time; null when the container could not be
    # parsed or the audio was registered from R2 without reading it.
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
