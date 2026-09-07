"""Bounded password work: no waiting queue and no early release on cancellation."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException, status


class PasswordWork:
    def __init__(self, workers: int) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="login-password"
        )
        self._slots = asyncio.Semaphore(workers)

    async def run[T, **P](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        if self._slots.locked():
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts")
        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        try:
            future = self._executor.submit(function, *args, **kwargs)
        except BaseException:
            self._slots.release()
            raise

        def release(_: object) -> None:
            # Release only when the actual thread exits, even if HTTP disconnected.
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._slots.release)

        future.add_done_callback(release)
        return await asyncio.shield(asyncio.wrap_future(future))

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
