"""Digest-only, bounded issuance bookkeeping. Visible committed storage always wins."""
from __future__ import annotations
from dataclasses import dataclass,replace
import threading
import time
from .registration_crypto import RegistrationPending,RegistrationUnavailable,RegistrationInvalid

@dataclass(frozen=True,repr=False)
class IssuanceEntry:
    digest: str
    intent_id: object
    address_id: object
    generation: int
    prior_id: object
    prior_generation: object
    keys: tuple
    started: float
    state: str="issuing"

class IssuanceMap:
    def __init__(self,clock=time.monotonic,capacity=128):
        self.clock=clock;self.capacity=capacity;self._entries={};self._lock=threading.Lock()
        self._epoch=0;self._latched=False;self._last_time=float('-inf')
    def _publish(self):
        if self._epoch==2**64-1:self._latched=True
        else:self._epoch+=1
    def add(self,entry):
        with self._lock:
            self._expire()
            if self._latched or len(self._entries)>=self.capacity or entry.digest in self._entries:
                raise RegistrationUnavailable()
            self._entries[entry.digest]=entry;self._publish()
    def resolve(self,entry):
        with self._lock:
            current=self._entries.get(entry.digest)
            if current and (current.intent_id,current.generation)==(entry.intent_id,entry.generation):
                del self._entries[entry.digest];self._publish()
    def uncertain(self,entry):
        with self._lock:
            current=self._entries.get(entry.digest)
            if current and (current.intent_id,current.generation)==(entry.intent_id,entry.generation):
                self._entries[entry.digest]=replace(current,state="uncertain");self._publish()
    def _expire(self):
        now=max(self.clock(),self._last_time);self._last_time=now
        for digest,e in list(self._entries.items()):
            age=now-e.started
            if age>=60:
                self._latched=True;del self._entries[digest];self._publish()
            elif age>=30 and e.state=="issuing":
                self._entries[digest]=replace(e,state="uncertain");self._publish()
    def snapshot(self):
        with self._lock:
            self._expire()
            return tuple(self._entries.values())
    def check(self,digest):
        with self._lock:
            self._expire()
            if digest in self._entries:raise RegistrationPending()
            if self._latched:raise RegistrationUnavailable(retry=True)
            return self._epoch
    def accepting(self):
        with self._lock:
            self._expire();return not self._latched
    def lookup(self,digest,read):
        # No map access before the initial committed read; a selected row is authoritative.
        row=read(digest)
        if row is not None:return row
        epoch=self.check(digest)
        for retry in range(2):
            row=read(digest)
            if row is not None:return row
            observed=self.check(digest)
            if observed==epoch:raise RegistrationInvalid()
            epoch=observed
        raise RegistrationPending()
