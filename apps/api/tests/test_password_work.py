import asyncio
import threading

import pytest
from fastapi import HTTPException

from daily_insights_api.modules.identity.password_work import PasswordWork


async def test_password_work_is_off_loop_and_cancellation_does_not_release_capacity() -> None:
    work = PasswordWork(1)
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    loop_thread = threading.get_ident()

    def blocking() -> int:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return threading.get_ident()

    task = asyncio.create_task(work.run(blocking))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        assert not task.done()
        with pytest.raises(HTTPException) as blocked:
            await work.run(lambda: None)
        assert blocked.value.status_code == 429
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(HTTPException):
            await work.run(lambda: None)
        release.set()
        async with asyncio.timeout(2):
            while True:
                try:
                    worker_thread = await work.run(threading.get_ident)
                    break
                except HTTPException:
                    await asyncio.sleep(0.001)
        assert worker_thread != loop_thread
        with pytest.raises(ValueError, match="test"):
            await work.run(lambda: int("test"))
        assert await work.run(lambda: "normal login") == "normal login"
    finally:
        release.set()
        work.close()
