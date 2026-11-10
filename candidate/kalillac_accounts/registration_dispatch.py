"""Bounded post-response issuance and independently supervised reconciliation."""
from __future__ import annotations
import os
import queue
import threading
import time
from sqlalchemy.exc import DBAPIError
from kalillac_server import FatalShutdown, FATAL_SHUTDOWN_SECONDS
from .registration_crypto import RegistrationUnavailable, RegistrationPending

class RegistrationDispatcher:
    def __init__(self,authority,*,capacity=128,workers=4,fatal=None):
        self.authority=authority;self._queue=queue.Queue(capacity);self._slots=threading.BoundedSemaphore(capacity)
        self._stop=threading.Event();self._threads=[];self._workers=workers
        self._fatal=fatal if fatal is not None else FatalShutdown()
        self._state_lock=threading.RLock();self._close_lock=threading.Lock()
        self._started=False;self._fatal_started=False;self._failed=False;self._closed=False
        self._io_thread=None;self._io_key=None;self._io_deadline=None;self._io_cancelled=False
        self._cleanup_at=time.monotonic()+60;self._latch_cancelled=False

    def _arm_fatal(self):
        # No failing thread allocation may leave an unarmed fatal process.
        try:
            with self._state_lock:
                if not self._fatal_started:
                    self._fatal.start();self._fatal_started=True
                self._fatal.trip()
        except BaseException:
            os._exit(1)

    def _drop_queued(self):
        while True:
            try:self._queue.get_nowait()
            except queue.Empty:return
            self._slots.release();self._queue.task_done()

    def _fail(self):
        with self._state_lock:
            if self._failed:return
            self._failed=True;self._stop.set()
            self._arm_fatal()
        try:self.authority.refuse_new_work()
        except BaseException:pass
        print("REGISTRATION_MAINTENANCE_FATAL",flush=True)
        # The independent fatal deadline is already armed before any I/O.
        try:
            self._drop_queued();self.authority.cancel_all()
        except BaseException:
            pass

    def _launch(self,target,name,*,io=False):
        with self._state_lock:
            if self._stop.is_set():return
            thread=None
            try:
                thread=threading.Thread(target=target,name=name,daemon=True)
                thread.start()
            finally:
                # start() can fail before native ownership exists. If a faulty
                # start raises after starting, retain that actual ownership too.
                if thread is not None and thread.ident is not None:
                    if io:self._io_thread=thread
                    else:self._threads.append(thread)
                elif io:
                    self._io_thread=None;self._io_key=None;self._io_deadline=None

    def start(self):
        with self._state_lock:
            if self._closed or self._stop.is_set():raise RuntimeError("REGISTRATION_DISPATCH_CLOSED")
            if self._started:return
            self._started=True
            try:
                self._fatal.start();self._fatal_started=True
                for _ in range(self._workers):self._launch(self._worker,"registration-dispatch")
                self._launch(self._maintenance,"registration-maintenance")
            except BaseException:
                self._fail()
                raise

    def submit(self,email):
        with self._state_lock:
            if self._stop.is_set() or not self.authority.markers.accepting() or not self._slots.acquire(blocking=False):
                print("REGISTRATION_DISPATCH_CAPACITY");return
            try:self._queue.put_nowait(email)
            except queue.Full:self._slots.release()

    def _worker(self):
        try:
            while not self._stop.is_set():
                try:email=self._queue.get(timeout=.25)
                except queue.Empty:continue
                try:
                    if not self._stop.is_set():self.authority.issue_after_response(email)
                except RegistrationUnavailable:
                    pass
                finally:
                    email=None;self._slots.release();self._queue.task_done()
        except BaseException:
            self._fail()

    def _owned_io(self,work):
        try:work()
        except (RegistrationUnavailable,RegistrationPending,DBAPIError):
            # Known retryable I/O outcomes retain marker reconciliation.
            pass
        except BaseException:
            self._fail()

    def _tick(self):
        entries=self.authority.markers.snapshot()
        if not self.authority.markers.accepting() and not self._latch_cancelled:
            self._latch_cancelled=True;self.authority.cancel_all()
        now=self.authority.monotonic()
        for entry in entries:
            if now-entry.started>=30:self.authority.cancel_issuance(entry)
        if self._io_thread is not None and self._io_thread.is_alive():
            if now>=self._io_deadline and not self._io_cancelled:
                self._io_cancelled=True;self.authority.cancel_operation(self._io_key)
            return
        uncertain=next((e for e in entries if e.state=="uncertain" and now-e.started<55),None)
        if uncertain is not None:
            self._io_key=("reconcile",uncertain.intent_id)
            self._io_deadline=min(now+5,uncertain.started+60)
            work=lambda:self.authority.reconcile(uncertain,deadline=self._io_deadline)
        elif time.monotonic()>=self._cleanup_at:
            self._cleanup_at=time.monotonic()+60
            self._io_key=("cleanup",None);self._io_deadline=now+30
            work=self.authority.cleanup_expired
        else:return
        self._io_cancelled=False
        self._launch(lambda:self._owned_io(work),"registration-owned-io",io=True)

    def _maintenance(self):
        try:
            while not self._stop.wait(.25):self._tick()
        except BaseException:
            self._fail()

    @staticmethod
    def _joinable(thread):
        return thread is not None and thread.ident is not None and thread is not threading.current_thread()

    def close(self):
        with self._close_lock:
            if self._closed:return
            with self._state_lock:
                self._stop.set();self.authority.refuse_new_work()
            self._drop_queued()
            deadline=time.monotonic()+(FATAL_SHUTDOWN_SECONDS if self._failed else 30)
            threads=list(self._threads)+[self._io_thread]
            for thread in threads:
                if self._joinable(thread):thread.join(max(0,deadline-time.monotonic()))
            if any(self._joinable(t) and t.is_alive() for t in threads):
                self._fail()
                # Retain ownership until actual completion or the prestarted
                # fatal supervisor's nonzero process exit (two-second bound).
                for thread in threads:
                    if self._joinable(thread):thread.join()
            self._closed=True
            self._fatal.close()
