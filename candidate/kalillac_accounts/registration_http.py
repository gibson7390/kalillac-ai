"""Exact V2 request/confirmation contracts; no account work before final 202 body."""
from __future__ import annotations
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from .registration_crypto import (strict_json,RegistrationInvalid,RegistrationUnavailable,RegistrationPending,RegistrationConflict)
from .registration_address import normalize_registration_email
from .rate_limit import client_identifier
from .passwords import password_length_ok,hash_password
from .password_work import PASSWORD_WORK_GATE,PasswordWorkBusy

def response(status=200,code=None,retry=None,background=None):
    headers={"Cache-Control":"no-store"}
    if retry is not None:headers["Retry-After"]=str(retry)
    return JSONResponse({"error":code} if code else {"ok":True},status_code=status,headers=headers,background=background)

async def body(request,fields):
    if request.headers.get("content-type","").split(";",1)[0].strip().lower()!="application/json":
        return response(415,"json_required")
    length=request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdigit():return response(400,"invalid_body")
        n=length.lstrip("0") or "0"
        if len(n)>4 or int(n)>4096:return response(413,"body_too_large")
    raw=bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>4096:return response(413,"body_too_large")
        raw.extend(chunk)
    try:obj=strict_json(bytes(raw))
    except RegistrationUnavailable:return response(400,"invalid_json")
    if not isinstance(obj,dict) or set(obj)!=fields:return response(400,"invalid_body")
    return obj

class RegistrationHTTP:
    def __init__(self,authority,dispatcher,settings,gate=None):
        self.authority=authority;self.dispatcher=dispatcher;self.settings=settings
        self.gate=gate if gate is not None else PASSWORD_WORK_GATE
    def client(self,request):
        return client_identifier(request.client.host if request.client else None,request.headers,self.settings.trust_cloudflare_client_ip)
    async def request(self,request):
        obj=await body(request,{"email"})
        if isinstance(obj,JSONResponse):return obj
        email=normalize_registration_email(obj["email"])
        if email is None:return response(422,"invalid_email")
        try:
            self.authority.ensure_accepting()
            decision=self.authority.limits.client(self.client(request))
            if not decision.allowed:return response(429,"too_many_attempts",decision.retry_after_seconds)
            return response(202,background=BackgroundTask(self.dispatcher.submit,email))
        except Exception:
            print("REGISTRATION_REQUEST_FAILED");return response(202)
    async def confirm(self,request):
        reservation=None;proof=None
        try:
            obj=await body(request,{"token","password"})
            if isinstance(obj,JSONResponse):return obj
            if not isinstance(obj["token"],str):return response(400,"invalid_body")
            password=obj["password"]
            if not isinstance(password,str):return response(422,"invalid_password")
            if not password_length_ok(password):return response(422,"password_length")
            decision=self.authority.limits.client(self.client(request),True)
            if not decision.allowed:return response(429,"too_many_attempts",decision.retry_after_seconds)
            proof=await run_in_threadpool(self.authority.prevalidate,obj["token"])
            decision,reservation=self.authority.limits.address(self.authority.ring.pairs(proof.email),"work")
            if not decision.allowed:return response(429,"too_many_attempts",decision.retry_after_seconds)
            def start_work():
                self.authority.ensure_accepting()
                reservation.charge()
            password_hash=await self.gate.run(hash_password,password,on_start=start_work)
            await run_in_threadpool(self.authority.confirm_after_hash,proof,password_hash)
            return response()
        except RegistrationInvalid:return response(400,"invalid_or_expired_token")
        except RegistrationPending:return response(503,"registration_pending",1)
        except RegistrationConflict:return response(409,"already_registered_sign_in")
        except PasswordWorkBusy:return response(503,"busy")
        except RegistrationUnavailable as error:return response(503,"registration_unavailable",1 if error.retry else None)
        except Exception:
            print("REGISTRATION_CONFIRM_FAILED");return response(503,"registration_unavailable",1)
        finally:
            if reservation is not None:reservation.release()
            proof=None

def confirmation_router(http):
    from fastapi import Request
    router=APIRouter(prefix="/register")
    @router.post("/confirm")
    async def confirm(request:Request):return await http.confirm(request)
    return router
