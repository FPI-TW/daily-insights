"""Keep podcast locks and streams alive until background SDK I/O has finished."""

import asyncio
from collections.abc import Awaitable


async def finish_io[T](operation: Awaitable[T]) -> T:
    """Drain SDK I/O before releasing locks/streams, even after repeated cancellation."""
    task = asyncio.ensure_future(operation)
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancellation = error
        except Exception:
            # The operation is finished and its error consumed. Preserve caller
            # cancellation after draining instead of suppressing shutdown.
            if cancellation is not None:
                raise cancellation from None
            raise
        else:
            if cancellation is not None:
                raise cancellation
            return result
