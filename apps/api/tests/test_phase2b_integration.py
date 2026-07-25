import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import BinaryIO

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api.core.config import Settings, get_settings
from daily_insights_api.core.enums import (
    AssetKind,
    AssetStatus,
    GenerationStatus,
    MessageRole,
    SystemRole,
    UserStatus,
)
from daily_insights_api.modules.assets.api import (
    AssetMigrationInput,
    AssetMigrationResult,
    migration_idempotency_key,
)
from daily_insights_api.modules.assets.models import Asset, AssetMigrationManifest
from daily_insights_api.modules.assets.object_store import ObjectMetadata, ObjectRef
from daily_insights_api.modules.assets.service import (
    AssetMigrationError,
    cutover_migration,
    migrate_podcast_assets,
)
from daily_insights_api.modules.chat.models import Conversation, Message
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.model_runtime.models import GenerationRecord, ModelConfiguration
from daily_insights_api.modules.podcasts.models import (
    PodcastEpisode,
    PodcastEpisodeAudioVariant,
)
from daily_insights_api.modules.tenancy.models import Membership, Organization
from daily_insights_api.scripts import migrate_podcast_assets as migration_cli

pytestmark = pytest.mark.integration


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[ObjectRef, tuple[bytes, str, str | None]] = {}

    def put(self, ref: ObjectRef, body: bytes, mime_type: str) -> None:
        self.objects[ref] = (body, mime_type, None)

    async def head(self, ref: ObjectRef) -> ObjectMetadata | None:
        value = self.objects.get(ref)
        if value is None:
            return None
        body, mime_type, sha256 = value
        return ObjectMetadata(ref=ref, size_bytes=len(body), mime_type=mime_type, sha256=sha256)

    async def copy_if_absent(
        self,
        source: ObjectRef,
        target: ObjectRef,
        *,
        sha256: str,
    ) -> bool:
        if target in self.objects:
            return False
        body, mime_type, _ = self.objects[source]
        self.objects[target] = (body, mime_type, sha256)
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
        if target in self.objects:
            return False
        body = content.read()
        assert len(body) == size_bytes
        self.objects[target] = (body, mime_type, sha256)
        return True

    async def overwrite(
        self,
        target: ObjectRef,
        content: BinaryIO,
        *,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> None:
        body = content.read()
        assert len(body) == size_bytes
        self.objects[target] = (body, mime_type, sha256)

    async def delete(self, target: ObjectRef) -> None:
        self.objects.pop(target, None)

    def read(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        body = self.objects[ref][0]

        async def chunks() -> AsyncIterator[bytes]:
            yield body

        return chunks()

    async def presign_get(self, ref: ObjectRef, expires_in: timedelta) -> str:
        del ref, expires_in
        raise AssertionError("migration integration does not sign")


def _entry(source_key: str) -> AssetMigrationInput:
    return AssetMigrationInput(
        asset_id=uuid.uuid4(),
        source=ObjectRef(bucket="legacy", key=source_key),
        target_bucket="canonical",
        trading_date=date(2026, 7, 24),
        locale="zh-hant",
        expected_mime_type="audio/mpeg",
    )


@dataclass(frozen=True)
class Phase2BDatabase:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    admin_id: uuid.UUID
    blocked_admin_id: uuid.UUID


def _remigrate_phase2b(database_url: str) -> None:
    previous = os.environ.get("DAILY_INSIGHTS_DATABASE_URL")
    os.environ["DAILY_INSIGHTS_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    configuration = Config(
        str(Path(__file__).parents[1] / "alembic.ini"),
    )
    try:
        engine = create_sync_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(text("DROP SCHEMA public CASCADE"))
                connection.execute(text("CREATE SCHEMA public"))
        finally:
            engine.dispose()
        command.upgrade(configuration, "head")
    finally:
        if previous is None:
            os.environ.pop("DAILY_INSIGHTS_DATABASE_URL", None)
        else:
            os.environ["DAILY_INSIGHTS_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def phase2b_database() -> AsyncIterator[Phase2BDatabase]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    await asyncio.to_thread(_remigrate_phase2b, database_url)
    settings = Settings(
        environment="test",
        database_url=database_url,
        session_secret=SecretStr("phase2b-session-secret"),
        password_pepper=SecretStr("phase2b-password-pepper"),
    )
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    blocked_admin_id = uuid.uuid4()
    async with session_factory.begin() as database:
        database.add_all(
            [
                User(
                    id=admin_id,
                    email=f"phase2b-admin-{admin_id}@example.com",
                    display_name="Phase 2B Admin",
                    password_hash="not-used",
                    must_change_password=False,
                    system_role=SystemRole.ADMIN,
                    status=UserStatus.ACTIVE,
                ),
                User(
                    id=blocked_admin_id,
                    email=f"phase2b-blocked-{blocked_admin_id}@example.com",
                    display_name="Blocked Admin",
                    password_hash="not-used",
                    must_change_password=True,
                    system_role=SystemRole.ADMIN,
                    status=UserStatus.ACTIVE,
                ),
            ]
        )
    try:
        yield Phase2BDatabase(settings, session_factory, admin_id, blocked_admin_id)
    finally:
        await engine.dispose()


async def _verified_migration(
    store: FakeObjectStore,
    *,
    source_key: str,
    trading_date: date,
) -> tuple[AssetMigrationInput, AssetMigrationResult]:
    entry = _entry(source_key)
    entry = entry.model_copy(update={"trading_date": trading_date})
    store.put(entry.source, b"podcast", "audio/mpeg")
    result = await migrate_podcast_assets(store, (entry,), dry_run=False)
    return entry, result


@pytest.mark.asyncio
async def test_cutover_commits_mapping_idempotently_then_emits_removal_artifact(
    phase2b_database: Phase2BDatabase,
    tmp_path: Path,
) -> None:
    store = FakeObjectStore()
    entry, verified = await _verified_migration(
        store,
        source_key="legacy/success.mp3",
        trading_date=date(2026, 7, 24),
    )
    async with phase2b_database.session_factory.begin() as database:
        await migration_cli.persist_migration_result(
            database,
            verified,
            actor_user_id=phase2b_database.admin_id,
        )

    verified_path = tmp_path / "verified.json"
    verified_path.write_text(verified.model_dump_json(), encoding="utf-8")
    output = tmp_path / "cutover.json"
    removal = tmp_path / "removal.json"
    arguments = migration_cli.parser().parse_args(
        [
            "--verified-manifest",
            str(verified_path),
            "--output",
            str(output),
            "--source-removal-manifest",
            str(removal),
            "--confirm-cutover",
            "--actor-user-id",
            str(phase2b_database.admin_id),
        ]
    )
    assert await migration_cli._execute(arguments, store, phase2b_database.settings) == 0
    assert output.is_file() and removal.is_file()
    removal_data = json.loads(removal.read_text())
    assert removal_data["sources"] == [{"bucket": entry.source.bucket, "key": entry.source.key}]

    second_output = tmp_path / "cutover-second.json"
    second_removal = tmp_path / "removal-second.json"
    arguments.output = second_output
    arguments.source_removal_manifest = second_removal
    assert await migration_cli._execute(arguments, store, phase2b_database.settings) == 0

    async with phase2b_database.session_factory() as database:
        assert await database.scalar(select(func.count()).select_from(PodcastEpisode)) == 1
        assert (
            await database.scalar(select(func.count()).select_from(PodcastEpisodeAudioVariant)) == 1
        )
        asset = await database.get(Asset, entry.asset_id)
        manifest = await database.scalar(
            select(AssetMigrationManifest).where(
                AssetMigrationManifest.idempotency_key == verified.idempotency_key
            )
        )
        assert asset is not None and asset.object_key == entry.target.key
        assert manifest is not None and manifest.status == "cutover"


@pytest.mark.asyncio
async def test_stale_object_and_unready_admin_fail_without_removal_output(
    phase2b_database: Phase2BDatabase,
    tmp_path: Path,
) -> None:
    store = FakeObjectStore()
    _, verified = await _verified_migration(
        store,
        source_key="legacy/stale.mp3",
        trading_date=date(2026, 7, 25),
    )
    async with phase2b_database.session_factory.begin() as database:
        with pytest.raises(AssetMigrationError, match="active_admin_required"):
            await migration_cli.persist_migration_result(
                database,
                verified,
                actor_user_id=phase2b_database.blocked_admin_id,
            )
    async with phase2b_database.session_factory.begin() as database:
        await migration_cli.persist_migration_result(
            database,
            verified,
            actor_user_id=phase2b_database.admin_id,
        )

    verified_path = tmp_path / "stale-verified.json"
    output = tmp_path / "stale-cutover.json"
    removal = tmp_path / "stale-removal.json"
    verified_path.write_text(verified.model_dump_json(), encoding="utf-8")
    store.put(verified.entries[0].target, b"badcast", "audio/mpeg")
    arguments = migration_cli.parser().parse_args(
        [
            "--verified-manifest",
            str(verified_path),
            "--output",
            str(output),
            "--source-removal-manifest",
            str(removal),
            "--confirm-cutover",
            "--actor-user-id",
            str(phase2b_database.admin_id),
        ]
    )
    with pytest.raises(AssetMigrationError, match="sha256_changed"):
        await migration_cli._execute(arguments, store, phase2b_database.settings)
    assert not output.exists()
    assert not removal.exists()


@pytest.mark.asyncio
async def test_cutover_conflict_rolls_back_all_new_mappings(
    phase2b_database: Phase2BDatabase,
) -> None:
    store = FakeObjectStore()
    first_entry, first = await _verified_migration(
        store,
        source_key="legacy/rollback-a.mp3",
        trading_date=date(2026, 7, 26),
    )
    second_entry, second = await _verified_migration(
        store,
        source_key="legacy/rollback-b.mp3",
        trading_date=date(2026, 7, 27),
    )
    combined = first.model_copy(
        update={
            "idempotency_key": migration_idempotency_key(
                (
                    first_entry,
                    second_entry,
                )
            ),
            "entries": first.entries + second.entries,
        }
    )
    async with phase2b_database.session_factory.begin() as database:
        await migration_cli.persist_migration_result(
            database,
            combined,
            actor_user_id=phase2b_database.admin_id,
        )
        database.add(
            Asset(
                id=second_entry.asset_id,
                bucket="wrong",
                object_key=f"conflict/{second_entry.asset_id}.mp3",
                kind=AssetKind.AUDIO,
                mime_type="audio/mpeg",
                size_bytes=7,
                sha256="0" * 64,
                locale="zh-hant",
                localized_titles={},
                status=AssetStatus.ACTIVE,
                uploaded_by_user_id=phase2b_database.admin_id,
            )
        )

    cutover = await cutover_migration(store, combined, confirmed=True)
    with pytest.raises(AssetMigrationError, match="existing_asset_mismatch"):
        async with phase2b_database.session_factory.begin() as database:
            await migration_cli.apply_database_cutover(
                database,
                cutover,
                actor_user_id=phase2b_database.admin_id,
            )
    async with phase2b_database.session_factory() as database:
        assert await database.get(Asset, first_entry.asset_id) is None
        dates = set(
            (
                await database.scalars(
                    select(PodcastEpisode.trading_date).where(
                        PodcastEpisode.trading_date.in_(
                            [first_entry.trading_date, second_entry.trading_date]
                        )
                    )
                )
            ).all()
        )
        assert dates == set()


@pytest.mark.asyncio
async def test_retained_history_triggers_enforce_lifecycle(
    phase2b_database: Phase2BDatabase,
) -> None:
    organization_id = uuid.uuid4()
    member_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    configuration_id = uuid.uuid4()
    generation_id = uuid.uuid4()
    async with phase2b_database.session_factory.begin() as database:
        database.add(
            Organization(
                id=organization_id,
                name="Trigger Org",
                slug=f"trigger-{organization_id}",
                seat_limit=1,
            )
        )
        database.add(
            User(
                id=member_id,
                email=f"trigger-{member_id}@example.com",
                display_name="Trigger Member",
                password_hash="not-used",
                must_change_password=False,
                system_role=SystemRole.ORG_MEMBER,
                status=UserStatus.ACTIVE,
            )
        )
        await database.flush()
        database.add(Membership(organization_id=organization_id, user_id=member_id))
        await database.flush()
        database.add(
            Conversation(
                id=conversation_id,
                organization_id=organization_id,
                user_id=member_id,
            )
        )
        database.add(
            ModelConfiguration(
                id=configuration_id,
                version=1,
                provider="deepseek",
                requested_model="test",
                prompt_version="v1",
                parameters={},
                created_by_user_id=phase2b_database.admin_id,
            )
        )
        await database.flush()
        database.add(
            Message(
                id=message_id,
                conversation_id=conversation_id,
                sequence_number=0,
                role=MessageRole.ASSISTANT,
                content="pending",
                status=GenerationStatus.PENDING,
            )
        )
        await database.flush()
        database.add(
            GenerationRecord(
                id=generation_id,
                message_id=message_id,
                model_configuration_id=configuration_id,
                model_configuration_version=1,
                provider="deepseek",
                requested_model="test",
                prompt_version="v1",
                parameters={},
                status=GenerationStatus.PENDING,
            )
        )

    async with phase2b_database.session_factory.begin() as database:
        await database.execute(
            text("UPDATE messages SET status = 'complete' WHERE id = :id"),
            {"id": message_id},
        )
        await database.execute(
            text("UPDATE generation_records SET status = 'complete' WHERE id = :id"),
            {"id": generation_id},
        )

    rejected = (
        ("UPDATE conversations SET title = 'changed' WHERE id = :id", conversation_id),
        ("DELETE FROM conversations WHERE id = :id", conversation_id),
        ("UPDATE messages SET content = 'changed' WHERE id = :id", message_id),
        ("DELETE FROM messages WHERE id = :id", message_id),
        (
            "UPDATE generation_records SET resolved_model = 'changed' WHERE id = :id",
            generation_id,
        ),
        ("DELETE FROM generation_records WHERE id = :id", generation_id),
        (
            "UPDATE model_configurations SET requested_model = 'changed' WHERE id = :id",
            configuration_id,
        ),
        ("DELETE FROM model_configurations WHERE id = :id", configuration_id),
    )
    for statement, record_id in rejected:
        with pytest.raises(DBAPIError):
            async with phase2b_database.session_factory.begin() as database:
                await database.execute(text(statement), {"id": record_id})
