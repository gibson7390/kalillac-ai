"""One process-local password-work permit; no host-wide or multi-worker claim.

The worker owns its permit through actual completion. Cancelling its caller
never cancels/abandons the executor future or admits a replacement early.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import threading

ADMISSION_WAIT_SECONDS = 2.0


class PasswordWorkBusy(Exception):
    pass


@dataclass(eq=False)
class _Waiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future
    active: bool = True
    granted: bool = False


class PasswordWorkGate:
    """Thread-safe handoff also supports independent test-client event loops."""

    def __init__(self, *, wait_for=asyncio.wait_for):
        self._lock = threading.Lock()
        self._available = True
        self._waiters = deque()
        self._wait_for = wait_for

    async def acquire(self):
        loop = asyncio.get_running_loop()
        waiter = _Waiter(loop, loop.create_future())
        with self._lock:
            if self._available:
                self._available = False
                return
            self._waiters.append(waiter)
        try:
            await self._wait_for(asyncio.shield(waiter.future), timeout=ADMISSION_WAIT_SECONDS)
        except BaseException as exc:
            with self._lock:
                waiter.active = False
                granted = waiter.granted
                if not granted:
                    self._waiters.remove(waiter)
            waiter.future.cancel()
            if granted:
                self.release()
            if isinstance(exc, TimeoutError):
                raise PasswordWorkBusy() from None
            raise

    def release(self):
        with self._lock:
            while self._waiters:
                waiter = self._waiters.popleft()
                if waiter.active:
                    waiter.granted = True
                    break
            else:
                if self._available:
                    raise RuntimeError("PASSWORD_WORK_DOUBLE_RELEASE")
                self._available = True
                return

        def deliver():
            if not waiter.future.done():
                waiter.future.set_result(None)

        waiter.loop.call_soon_threadsafe(deliver)

    async def run(self, function, *args, on_start=None):
        # A scheduling checkpoint delivers pending entry cancellation before
        # admission. A previously handled cancellation count is not a new one.
        await asyncio.sleep(0)
        await self.acquire()
        state_lock = threading.Lock()
        state = {"cancelled": False, "started": False}
        caller = asyncio.current_task()
        entry_cancellations = caller.cancelling()

        def worker():
            try:
                with state_lock:
                    if state["cancelled"] or caller.cancelling() > entry_cancellations:
                        return None
                    state["started"] = True
                    if on_start is not None:
                        on_start()
                return function(*args)
            finally:
                self.release()

        try:
            future = asyncio.get_running_loop().run_in_executor(None, worker)
        except BaseException:
            self.release()
            raise
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            with state_lock:
                if not state["started"]:
                    state["cancelled"] = True
            # Drain the owned worker even after repeated cancellation. Its
            # result/exception is observed; the original cancellation wins.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not future.cancelled():
                future.exception()
            raise


# All current request-time routers share this exact process-local object.
PASSWORD_WORK_GATE = PasswordWorkGate()
