"""Bounded process-local registration ledgers; no request or resend resets charges."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass,field
import hashlib
import math
import threading
import time
from .rate_limit import AttemptDecision
from .registration_crypto import RegistrationUnavailable

@dataclass
class _Bucket:
    events: dict=field(default_factory=lambda: {"link":deque(),"notice":deque(),"work":deque()})
    reservations: int=0
    aliases: set=field(default_factory=set)

RULES={"link":(3,86400),"notice":(1,86400),"work":(5,900)}

class WorkReservation:
    def __init__(self,store,key):
        self._store,self._key=store,key
        self._done=False
        self._lock=threading.Lock()
    def charge(self):
        with self._lock:
            if self._done:raise RegistrationUnavailable()
            self._store._finish(self._key,True);self._done=True
    def release(self):
        with self._lock:
            if not self._done:self._store._finish(self._key,False);self._done=True

class RegistrationLimits:
    def __init__(self,clock=time.monotonic,max_keys=100000):
        self.clock=clock;self.max_keys=max_keys;self._now=float("-inf");self._lock=threading.Lock()
        self._request={};self._confirm={};self._buckets={};self._aliases={}
    def _advance(self):
        self._now=max(self._now,self.clock());return self._now
    def _trim(self,events,window):
        while events and events[0]<=self._now-window:events.popleft()
    def client(self,client,confirmation=False):
        key=hashlib.sha256(client.encode("utf-8")).hexdigest()
        with self._lock:
            now=self._advance();store=self._confirm if confirmation else self._request
            maximum=10 if confirmation else 5
            for k,events in list(store.items()):
                self._trim(events,900)
                if not events:del store[k]
            if key not in store:
                if len(store)>=self.max_keys:
                    retry=min(math.ceil(events[-1]+900-now) for events in store.values())
                    return AttemptDecision(False,max(1,min(900,retry)))
                store[key]=deque()
            events=store[key]
            if len(events)>=maximum:
                return AttemptDecision(False,max(1,min(900,math.ceil(events[0]+900-now))))
            events.append(now)
            return AttemptDecision(True)
    def _sweep(self):
        for key,b in list(self._buckets.items()):
            for scope,(_,window) in RULES.items():self._trim(b.events[scope],window)
            if not b.reservations and not any(b.events.values()):
                for alias in b.aliases:del self._aliases[alias]
                del self._buckets[key]
    def _bucket(self,pairs):
        self._sweep()
        aliases=set(pairs)
        if not 1<=len(aliases)<=8:raise RegistrationUnavailable()
        matches={self._aliases[a] for a in aliases if a in self._aliases}
        if len(matches)>1:raise RegistrationUnavailable()
        if matches:key=next(iter(matches))
        else:
            if len(self._buckets)>=self.max_keys:
                return None,None
            key=object();self._buckets[key]=_Bucket()
        b=self._buckets[key]
        if len(b.aliases|aliases)>8:raise RegistrationUnavailable()
        for alias in aliases:self._aliases[alias]=key
        b.aliases.update(aliases)
        return key,b
    def address(self,pairs,scope):
        with self._lock:
            now=self._advance();key,b=self._bucket(pairs)
            maximum,window=RULES[scope]
            if b is None:
                deadlines=[max(events[-1]+RULES[s][1] for s,events in item.events.items() if events) for item in self._buckets.values() if any(item.events.values())]
                retry=1 if any(item.reservations for item in self._buckets.values()) else max(1,math.ceil(min(deadlines)-now))
                return AttemptDecision(False,min(window,retry)),None
            events=b.events[scope]
            if len(events)+(b.reservations if scope=="work" else 0)>=maximum:
                retry=math.ceil(events[0]+window-now) if events else 1
                return AttemptDecision(False,max(1,min(window,retry))),None
            if scope=="work":
                b.reservations+=1
                return AttemptDecision(True),WorkReservation(self,key)
            events.append(now)
            return AttemptDecision(True),None
    def _finish(self,key,charge):
        with self._lock:
            now=self._advance();b=self._buckets[key]
            if b.reservations<1:raise RegistrationUnavailable()
            b.reservations-=1
            if charge:b.events["work"].append(now)
            self._sweep()
