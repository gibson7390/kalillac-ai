"""Private registration authority. This component alone owns the isolated A factory."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass,field
from datetime import datetime,timedelta,timezone
import threading
import time
import uuid
from sqlalchemy import func,or_,and_,select,text
from sqlalchemy.exc import IntegrityError,DBAPIError
from kalillac_db.models import AccountRegistrationIntent as Intent,AccountRegistrationActivation as Activation,User,AccountEntitlement
from .registration_crypto import (RegistrationInvalid,RegistrationUnavailable,RegistrationConflict,
    advisory_keys,new_token,token_digest)
from .registration_markers import IssuanceEntry,IssuanceMap
from .registration_limits import RegistrationLimits
from .registration_address import normalize_registration_email

def utc_now():return datetime.now(timezone.utc)
def aware(value):return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
def clear(row,state,now):
    row.state=state;row.is_current=False
    row.email_ciphertext=row.email_nonce=row.email_key_version=None
    setattr(row,state+"_at",now)

@dataclass(frozen=True,repr=False)
class _Proof:
    intent_id: uuid.UUID
    digest: str
    address_id: uuid.UUID
    generation: int
    email: str
    binding: tuple
    ring: object=field(repr=False)
    owner: object=field(repr=False)
    used: list=field(default_factory=list,repr=False,compare=False)
    lock: object=field(default_factory=threading.Lock,repr=False,compare=False)

class RegistrationAuthority:
    def __init__(self,factory,ring,gateway,url,*,role="kalillac_registration",limits=None,markers=None,clock=utc_now,monotonic=time.monotonic,production=True):
        self.__factory=factory;self.ring=ring;self.gateway=gateway;self.url=url;self.role=role
        self.limits=limits if limits is not None else RegistrationLimits()
        self.markers=markers if markers is not None else IssuanceMap()
        self.clock=clock;self.monotonic=monotonic;self.production=production
        self.__owner=object();self._connections={};self._connections_lock=threading.Lock()
        self._stopping=threading.Event()
    def __repr__(self):return "RegistrationAuthority()"
    def refuse_new_work(self):self._stopping.set()
    def ensure_accepting(self):
        if self._stopping.is_set():raise RegistrationUnavailable()
    @contextmanager
    def _transaction(self,*,reconcile=False,operation=None,outcome=None,deadline=None):
        self.ensure_accepting()
        session=self.__factory()
        transaction=session.begin()
        sql_connection=None
        driver_connection=None
        try:
            sql_connection=session.connection()
            driver_connection=sql_connection.connection.driver_connection
            if operation is not None:
                with self._connections_lock:self._connections[operation]=driver_connection
            if deadline is not None and self.monotonic()>=deadline:raise RegistrationUnavailable(retry=True)
            if session.get_bind().dialect.name=="postgresql":
                who=session.execute(text("SELECT session_user,current_user,system_user")).one()
                if who[0]!=self.role or who[1]!=self.role or (self.production and who[2]!="scram-sha-256:"+self.role):
                    raise RegistrationUnavailable()
                session.execute(text("SET LOCAL lock_timeout='"+("2s" if reconcile else "5s")+"'"))
                session.execute(text("SET LOCAL statement_timeout='"+("5s" if reconcile else "30s")+"'"))
                session.execute(text("SET LOCAL idle_in_transaction_session_timeout='30s'"))
            elif session.get_bind().dialect.name!="sqlite" or self.production:
                raise RegistrationUnavailable()
            yield session
            if deadline is not None and self.monotonic()>=deadline:raise RegistrationUnavailable(retry=True)
            if outcome is not None:outcome["commit_started"]=True
            transaction.commit()
        except BaseException as error:
            if outcome is not None:outcome["known_rollback"]=not outcome.get("commit_started",False) and not isinstance(error,DBAPIError)
            if session.get_bind().dialect.name=="postgresql" and driver_connection is not None:
                # Invalidate the owned physical connection before rollback can
                # return it to the pool. Never recycle an ambiguous protocol.
                try:
                    if sql_connection is not None and not sql_connection.closed:sql_connection.invalidate()
                    else:driver_connection.close()
                except Exception:
                    try:driver_connection.close()
                    except Exception:pass
            try:session.rollback()
            except Exception:pass
            raise
        finally:
            if operation is not None:
                with self._connections_lock:self._connections.pop(operation,None)
            session.close()
    def cancel_operation(self,key):
        with self._connections_lock:connection=self._connections.get(key)
        if connection is not None:
            try:connection.cancel_safe(timeout=.5)
            except Exception:pass
    def cancel_issuance(self,entry):self.cancel_operation(entry.intent_id)
    def cancel_all(self):
        with self._connections_lock:keys=tuple(self._connections)
        for key in keys:self.cancel_operation(key)
    def _control(self,session,exclusive=False):
        if session.get_bind().dialect.name=="postgresql":
            session.execute(text("SELECT kalillac.registration_control_lock(:x)"),{"x":exclusive})
        row=session.get(Activation,1)
        if row is None or row.state not in ("unactivated","active_zero_legacy") or (self.production and row.state!="active_zero_legacy"):
            raise RegistrationUnavailable()
    def _locks(self,session,keys):
        if session.get_bind().dialect.name=="postgresql":
            for key in keys:session.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(CAST(:k AS bigint))"),{"k":key})
    def _time(self,session):
        return session.execute(select(func.clock_timestamp())).scalar_one() if session.get_bind().dialect.name=="postgresql" else self.clock()
    def _read(self,digest):
        try:
            with self._transaction() as session:
                row=session.scalar(select(Intent).where(Intent.token_digest==digest))
                if row is not None:session.expunge(row)
                return row
        except Exception:raise RegistrationUnavailable(retry=True) from None
    @staticmethod
    def _binding(row):
        return (row.address_lookup_key_version,row.address_lookup_digest,row.email_key_version,
                bytes(row.email_nonce or b""),bytes(row.email_ciphertext or b""))
    def _validate(self,row):
        if row is None or row.purpose!="finish_registration":raise RegistrationInvalid()
        if row.state in ("consumed","superseded","expired") or aware(row.expires_at)<=self.clock():
            raise RegistrationInvalid()
        if row.state!="pending" or not row.is_current or row.format_version!=1:
            raise RegistrationUnavailable()
        return self.ring.decrypt(row)
    def prevalidate(self,token):
        digest=token_digest(token)
        row=self.markers.lookup(digest,self._read)
        email=self._validate(row)
        return _Proof(row.id,digest,row.address_id,row.generation,email,self._binding(row),self.ring,self.__owner)
    def confirm_after_hash(self,proof,password_hash):
        if not isinstance(proof,_Proof) or proof.owner is not self.__owner or proof.ring is not self.ring:
            raise RegistrationUnavailable()
        with proof.lock:
            if proof.used:raise RegistrationInvalid()
            proof.used.append(True)
        try:
            with self._transaction() as session:
                self._control(session)
                self._locks(session,advisory_keys(self.ring.pairs(proof.email)))
                row=session.scalar(select(Intent).where(Intent.id==proof.intent_id,Intent.token_digest==proof.digest).with_for_update())
                now=self._time(session)
                if row is not None and row.state=="consumed":
                    if session.scalar(select(User.id).where(User.email==proof.email)) is not None:raise RegistrationConflict()
                    raise RegistrationInvalid()
                if row is None or aware(row.expires_at)<=now:raise RegistrationInvalid()
                email=self._validate(row)
                if (email!=proof.email or row.address_id!=proof.address_id or row.generation!=proof.generation or self._binding(row)!=proof.binding):
                    raise RegistrationUnavailable()
                if session.scalar(select(User.id).where(User.email==email)) is not None:raise RegistrationConflict()
                clear(row,"consumed",now)
                if session.get_bind().dialect.name=="sqlite":row.consumed_xid="test-only"
                session.flush()
                user=User(id=uuid.uuid4(),email=email,password_hash=password_hash,email_verified_at=now,is_active=True,registration_intent_id=row.id,quarantined_at=None,sessions_valid_after=None)
                session.add(user);session.flush()
                if session.get_bind().dialect.name=="sqlite":row.provenance_claimed=True
                session.add(AccountEntitlement(user_id=user.id,tier="free",source="registration",expires_at=None))
                session.flush()
        except IntegrityError as error:
            if getattr(error.orig,"sqlstate",None)=="23505" and getattr(getattr(error.orig,"diag",None),"constraint_name",None)=="uq_users_email":
                raise RegistrationConflict() from None
            raise RegistrationUnavailable() from None
    def issue_after_response(self,email):
        email=normalize_registration_email(email)
        if email is None:return
        self.ensure_accepting()
        entry=None;outcome={}
        try:
            if not self.markers.accepting():return
            pairs=self.ring.pairs(email);keys=advisory_keys(pairs)
            operation=uuid.uuid4()
            with self._transaction(operation=operation,outcome=outcome) as session:
                self._control(session);self._locks(session,keys)
                exists=session.scalar(select(User.id).where(User.email==email)) is not None
                decision,_=self.limits.address(pairs,"notice" if exists else "link")
                if not decision.allowed:return
                if exists:
                    self.gateway.send_registration_notice(email)
                    return
                rows=list(session.scalars(select(Intent).where(or_(*(and_(Intent.address_lookup_key_version==v,Intent.address_lookup_digest==d) for v,d in pairs))).with_for_update()))
                ids={r.address_id for r in rows}
                if len(ids)>1 or sum(r.is_current for r in rows)>1:raise RegistrationUnavailable()
                address_id=next(iter(ids)) if ids else uuid.uuid4()
                generation=max((r.generation for r in rows),default=0)+1
                prior=next((r for r in rows if r.is_current),None)
                now=self._time(session)
                if prior is not None:
                    clear(prior,"superseded",now);session.flush()
                token=new_token();digest=token_digest(token)
                active=dict(pairs)[self.ring.active_lookup]
                row=Intent(id=operation,address_id=address_id,generation=generation,purpose="finish_registration",format_version=1,
                    address_lookup_key_version=self.ring.active_lookup,address_lookup_digest=active,token_digest=digest,is_current=True,
                    state="pending",created_at=now,expires_at=now+timedelta(hours=24))
                self.ring.encrypt(row,email)
                session.add(row);session.flush()
                entry=IssuanceEntry(digest,row.id,address_id,generation,prior.id if prior else None,prior.generation if prior else None,keys,self.monotonic())
                self.markers.add(entry)
                self.gateway.send_registration_finish(email,self.url+"#token="+token)
            self.markers.resolve(entry)
        except Exception:
            # Delivery/flush errors known to roll back are resolved only after the
            # same fresh lock protocol; a COMMIT failure can be ambiguous.
            if entry is not None:
                if outcome.get("known_rollback"):self.markers.resolve(entry)
                else:self.markers.uncertain(entry)
            print("REGISTRATION_ISSUANCE_UNCERTAIN" if entry else "REGISTRATION_ISSUANCE_FAILED")
    def reconcile(self,entry,deadline=None):
        deadline=min(self.monotonic()+5,entry.started+60) if deadline is None else deadline
        if self.monotonic()>=deadline:return False
        try:
            with self._transaction(reconcile=True,operation=("reconcile",entry.intent_id),deadline=deadline) as session:
                self._control(session);self._locks(session,entry.keys)
                row=session.scalar(select(Intent).where(Intent.id==entry.intent_id))
                if row is not None:
                    if (row.token_digest,row.address_id,row.generation)!=(entry.digest,entry.address_id,entry.generation):return False
                elif entry.prior_id is not None:
                    prior=session.get(Intent,entry.prior_id)
                    if prior is None or prior.generation!=entry.prior_generation or not prior.is_current:return False
                else:
                    if session.scalar(select(Intent.id).where(Intent.address_id==entry.address_id)) is not None:return False
            if self.monotonic()>=deadline:return False
            self.markers.resolve(entry);return True
        except Exception:return False
    def cleanup_expired(self):
        try:
            with self._transaction(operation=("cleanup",None)) as session:
                self._control(session);now=self._time(session)
                rows=session.scalars(select(Intent).where(Intent.expires_at<=now).order_by(Intent.expires_at,Intent.id).limit(250).with_for_update(skip_locked=True))
                for row in rows:session.delete(row)
        except Exception:print("REGISTRATION_CLEANUP_FAILED")
