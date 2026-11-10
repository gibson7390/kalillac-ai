"""Deterministic tests of the production password-work gate."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from kalillac_accounts.password_work import ADMISSION_WAIT_SECONDS, PasswordWorkBusy, PasswordWorkGate


async def checkpoint():
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


def test_exact_admission_timeout_and_capacity_recovery():
    seen = []
    async def timeout(future, *, timeout):
        seen.append(timeout)
        future.cancel()
        raise TimeoutError()
    async def scenario():
        gate = PasswordWorkGate(wait_for=timeout)
        await gate.acquire()
        with pytest.raises(PasswordWorkBusy):
            await gate.run(lambda: pytest.fail("timed-out job ran"))
        assert seen == [2.0] and ADMISSION_WAIT_SECONDS == 2.0
        gate.release()
        assert await gate.run(lambda: 42) == 42
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["before_admission", "waiting", "handoff"])
def test_cancelled_waiter_never_runs_or_leaks_capacity(stage):
    async def scenario():
        queued = asyncio.Event()
        async def wait(future, *, timeout):
            assert timeout == 2.0
            queued.set()
            return await future
        gate = PasswordWorkGate(wait_for=wait)
        await gate.acquire()
        task = asyncio.create_task(gate.run(lambda: pytest.fail("cancelled waiter ran")))
        if stage != "before_admission":
            await asyncio.wait_for(queued.wait(), 5)
        if stage == "handoff":
            gate.release()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if stage != "handoff":
            gate.release()
        assert await gate.run(lambda: "next") == "next"
    asyncio.run(scenario())


@pytest.mark.parametrize("fails", [False, True])
def test_cancellation_drains_actual_worker_before_replacement(fails):
    async def scenario():
        loop = asyncio.get_running_loop()
        entered, queued = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        calls = []
        async def wait(future, *, timeout):
            queued.set()
            return await future
        gate = PasswordWorkGate(wait_for=wait)
        def work():
            calls.append("first")
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            if fails:
                raise ValueError("synthetic worker failure")
            return "done"
        first = asyncio.create_task(gate.run(work))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            first.cancel()
            await checkpoint()
            second = asyncio.create_task(gate.run(lambda: calls.append("second")))
            await asyncio.wait_for(queued.wait(), 5)
            first.cancel()  # Repeated cancellation cannot detach the worker.
            await checkpoint()
            assert calls == ["first"] and not first.done() and not second.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, 5)
        assert calls == ["first", "second"]
        assert await gate.run(lambda: 7) == 7
    asyncio.run(scenario())


def test_cancel_before_executor_worker_starts_is_uncharged():
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        occupied, acquired = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        def occupy():
            loop.call_soon_threadsafe(occupied.set)
            assert release.wait(5)
        occupying = loop.run_in_executor(None, occupy)
        await asyncio.wait_for(occupied.wait(), 5)
        class ObservedGate(PasswordWorkGate):
            async def acquire(self):
                await super().acquire()
                acquired.set()
        gate = ObservedGate()
        charged = []
        task = asyncio.create_task(gate.run(
            lambda: pytest.fail("cancelled queued worker ran"),
            on_start=lambda: charged.append(True),
        ))
        try:
            await asyncio.wait_for(acquired.wait(), 5)
            task.cancel()
            await checkpoint()
            assert charged == [] and not task.done()
        finally:
            release.set()
        await occupying
        with pytest.raises(asyncio.CancelledError):
            await task
        assert charged == []
        assert await gate.run(lambda: 9) == 9
    asyncio.run(scenario())


def test_worker_exception_releases_once():
    async def scenario():
        gate = PasswordWorkGate()
        def fail():
            raise ValueError("synthetic failure")
        with pytest.raises(ValueError):
            await gate.run(fail)
        assert await gate.run(lambda: "ok") == "ok"
        with pytest.raises(RuntimeError, match="DOUBLE_RELEASE"):
            gate.release()
    asyncio.run(scenario())


def test_executor_submission_failure_releases(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        original = loop.run_in_executor
        def fail(*args):
            raise RuntimeError("synthetic submission failure")
        gate = PasswordWorkGate()
        monkeypatch.setattr(loop, "run_in_executor", fail)
        with pytest.raises(RuntimeError, match="submission"):
            await gate.run(lambda: None)
        monkeypatch.setattr(loop, "run_in_executor", original)
        assert await gate.run(lambda: "ok") == "ok"
    asyncio.run(scenario())


def test_already_handled_cancellation_does_not_suppress_later_work():
    async def scenario():
        caller = asyncio.current_task()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.Event().wait()
        assert caller.cancelling() == 1
        gate = PasswordWorkGate()
        assert await gate.run(lambda: "still usable") == "still usable"
        caller.uncancel()
    asyncio.run(scenario())


def test_pending_cancellation_at_entry_never_admits_password_work():
    async def scenario():
        caller = asyncio.current_task()
        gate = PasswordWorkGate()
        calls = []
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gate.run(lambda: calls.append(True), on_start=lambda: calls.append("charged"))
        assert calls == []
        caller.uncancel()
        assert await gate.run(lambda: "next") == "next"
    asyncio.run(scenario())


def test_default_wait_for_uses_exact_budget_and_handles_timer_expiry(monkeypatch):
    # Exercise the real asyncio.wait_for path, replacing only its timer
    # factory with immediate expiry rather than waiting on wall time.
    observed = []
    real_timeout = asyncio.timeouts.timeout
    def expire(delay):
        observed.append(delay)
        return real_timeout(0)
    async def scenario():
        gate = PasswordWorkGate()
        await gate.acquire()
        with monkeypatch.context() as timer:
            timer.setattr(asyncio.timeouts, "timeout", expire)
            with pytest.raises(PasswordWorkBusy):
                await gate.run(lambda: pytest.fail("expired admission ran"))
            assert observed == [2.0]
        gate.release()
        assert await gate.run(lambda: "capacity restored") == "capacity restored"
    asyncio.run(scenario())
