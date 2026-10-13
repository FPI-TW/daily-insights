"""Durable, retryable removal of a complete unpublished episode.

Persist the frozen episode and exact registered object inventory before any
external deletion. Commit each object's progress separately. DB failure after
an external delete is safe: the next request repeats an idempotent delete.
Only the final transaction removes episode/variant/translation/asset rows.
"""

import uuid
from datetime import UTC, datetime

from botocore.exceptions import ClientError
from fastapi import HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.assets.api import Asset, ObjectRef, ObjectStore
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.podcasts.io import finish_io
from daily_insights_api.modules.podcasts.lifecycle import lock_upload_date, lock_upload_object
from daily_insights_api.modules.podcasts.models import (
    PodcastDateGeneration,
    PodcastDeletionJob,
    PodcastDeletionObject,
    PodcastEpisode,
    PodcastEpisodeAudioVariant,
)


async def _shared(database: AsyncSession, asset_id: uuid.UUID, episode_id: uuid.UUID) -> bool:
    audio = await database.scalar(
        select(PodcastEpisodeAudioVariant.id)
        .where(
            PodcastEpisodeAudioVariant.asset_id == asset_id,
            PodcastEpisodeAudioVariant.episode_id != episode_id,
        )
        .limit(1)
    )
    cover = await database.scalar(
        select(PodcastEpisode.id)
        .where(
            PodcastEpisode.cover_asset_id == asset_id,
            PodcastEpisode.id != episode_id,
        )
        .limit(1)
    )
    return audio is not None or cover is not None


async def _delete_object(store: ObjectStore, target: PodcastDeletionObject) -> None:
    try:
        await finish_io(store.delete(ObjectRef(bucket=target.bucket, key=target.object_key)))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in {"NoSuchKey", "NotFound", "404"}:
            raise


async def remove_episode(
    database: AsyncSession,
    store: ObjectStore,
    episode_id: uuid.UUID,
    expected_version: int,
    actor_user_id: uuid.UUID,
    request_id: str,
) -> None:
    # Look up the immutable date before taking any row/object lock.
    trading_date = await database.scalar(
        select(PodcastEpisode.trading_date).where(PodcastEpisode.id == episode_id)
    )
    if trading_date is None:
        job = await database.scalar(
            select(PodcastDeletionJob).where(PodcastDeletionJob.episode_id == episode_id)
        )
        if (
            job is not None
            and job.status == "completed"
            and expected_version in {job.requested_version, job.episode_version}
        ):
            return  # Lost final response replay; never acts on a replacement episode.
        raise HTTPException(404, detail="Podcast episode not found")
    await lock_upload_date(database, trading_date)
    episode = await database.scalar(
        select(PodcastEpisode)
        .where(PodcastEpisode.id == episode_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if episode is None:
        # A concurrent remover finished while this request waited for the date.
        job = await database.scalar(
            select(PodcastDeletionJob).where(PodcastDeletionJob.episode_id == episode_id)
        )
        if (
            job is not None
            and job.status == "completed"
            and expected_version in {job.requested_version, job.episode_version}
        ):
            return
        raise HTTPException(404, detail="Podcast episode not found")
    if episode.version != expected_version:
        raise HTTPException(
            409, detail={"code": "episode_version_conflict", "current_version": episode.version}
        )
    if episode.status != "draft":
        raise HTTPException(
            409, detail={"code": "episode_must_be_unpublished", "current_version": episode.version}
        )
    job = await database.scalar(
        select(PodcastDeletionJob)
        .where(PodcastDeletionJob.episode_id == episode_id)
        .with_for_update()
    )
    if job is None:
        episode.deletion_pending = True
        episode.version += 1
        generation = await database.get(PodcastDateGeneration, trading_date)
        if generation is None:
            generation = PodcastDateGeneration(trading_date=trading_date, generation=1)
            database.add(generation)
        else:
            generation.generation += 1
        job = PodcastDeletionJob(
            episode_id=episode.id,
            trading_date=trading_date,
            episode_version=episode.version,
            requested_version=expected_version,
            requested_by_user_id=actor_user_id,
        )
        database.add(job)
        await database.flush()
        asset_ids = select(PodcastEpisodeAudioVariant.asset_id).where(
            PodcastEpisodeAudioVariant.episode_id == episode_id
        )
        assets = (
            await database.scalars(
                select(Asset)
                .where(or_(Asset.id.in_(asset_ids), Asset.id == episode.cover_asset_id))
                .order_by(Asset.id)
            )
        ).all()
        for registered_asset in assets:
            database.add(
                PodcastDeletionObject(
                    job_id=job.id,
                    asset_id=registered_asset.id,
                    bucket=registered_asset.bucket,
                    object_key=registered_asset.object_key,
                )
            )
        record_audit_event(
            database,
            actor_user_id=actor_user_id,
            action="podcast.episode_removal_started",
            target_type="podcast_episode",
            target_id=str(episode_id),
            before={"version": expected_version},
            after={
                "version": episode.version,
                "trading_date": trading_date.isoformat(),
                "objects": len(assets),
            },
            request_id=request_id,
        )
    job_id = job.id
    await database.commit()  # Freeze/inventory must be durable before touching storage.

    while True:
        await lock_upload_date(database, trading_date)
        job = await database.scalar(
            select(PodcastDeletionJob)
            .where(PodcastDeletionJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert job is not None
        if job.status == "completed":
            await database.commit()
            return
        target = await database.scalar(
            select(PodcastDeletionObject)
            .where(
                PodcastDeletionObject.job_id == job_id, PodcastDeletionObject.status == "pending"
            )
            .order_by(PodcastDeletionObject.asset_id)
            .limit(1)
            .with_for_update()
        )
        if target is None:
            break
        await lock_upload_object(database, target.asset_id)
        asset = await database.scalar(
            select(Asset).where(Asset.id == target.asset_id).with_for_update()
        )
        if await _shared(database, target.asset_id, episode_id):
            target.status = "shared"
        else:
            if asset is not None and (
                asset.bucket != target.bucket or asset.object_key != target.object_key
            ):
                raise RuntimeError("registered removal object identity changed")
            await _delete_object(store, target)
            target.status = "deleted"
        await database.commit()

    # Early shared classification is provisional: another date's removal can
    # drop the last other reference before this final transaction. Hold every
    # object lock in sorted order through the reference deletion and commit.
    # The last remover must clear storage before reporting completion.
    targets = (
        await database.scalars(
            select(PodcastDeletionObject)
            .where(PodcastDeletionObject.job_id == job_id)
            .order_by(PodcastDeletionObject.asset_id)
        )
    ).all()
    for target in targets:
        await lock_upload_object(database, target.asset_id)
    for target in targets:
        if target.status != "shared" or await _shared(database, target.asset_id, episode_id):
            continue
        asset = await database.scalar(
            select(Asset).where(Asset.id == target.asset_id).with_for_update()
        )
        if asset is not None and (
            asset.bucket != target.bucket or asset.object_key != target.object_key
        ):
            raise RuntimeError("registered removal object identity changed")
        await _delete_object(store, target)
        target.status = "deleted"
    await database.execute(delete(PodcastEpisode).where(PodcastEpisode.id == episode_id))
    for target in targets:
        if target.status == "deleted" and not await _shared(database, target.asset_id, episode_id):
            await database.execute(
                delete(Asset).where(
                    Asset.id == target.asset_id,
                    Asset.bucket == target.bucket,
                    Asset.object_key == target.object_key,
                )
            )
    job.status = "completed"
    job.completed_at = datetime.now(UTC)
    record_audit_event(
        database,
        actor_user_id=actor_user_id,
        action="podcast.episode_removed",
        target_type="podcast_episode",
        target_id=str(episode_id),
        after={"job_id": str(job.id), "trading_date": trading_date.isoformat()},
        request_id=request_id,
    )
    await database.commit()
