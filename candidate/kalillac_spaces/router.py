"""/api/spaces: Spaces, instructions, conversations and persistent sends.

Access, in order, for every route:
  1. no valid account session            -> 401 not_authenticated
  2. account not on the beta allowlist   -> 404 not_found (undiscoverable)
  3. email not verified                  -> 403 email_verification_required
Unsafe methods then require a same-origin request (403 origin_rejected) and,
where a body is expected, a JSON object no larger than 16 KiB (415, 413)
with no unknown fields (422). On every route, a query parameter the route
does not define, or any repeated one, is 422 invalid_request before any body
is read or anything changes. Another account's object and a missing one
are the same 404. Every response is no-store and varies by Cookie.

A send runs under one RequestBudget from authorization to persistence
(504 request_timeout when it runs out, 499 request_cancelled when a delete
cancels it); every transaction is bounded by what remains of it, and it is
checked again once the rows are locked. Its lease carries a fresh server
attempt token, separate from the client's idempotency key, which completion
and cleanup must present. The quota charge is written in the same
transaction as the saved exchange.

Logging: fixed codes only. No ids, names, emails, instructions or message
text are ever printed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import functools
import math
import threading
from typing import Any, Awaitable, Callable
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from kalillac_accounts.auth import (
    AuthContext,
    RequestRejected,
    read_json_object,
    require_same_origin,
    resolve_account,
    session_token_hash,
)
from kalillac_db.engine import get_session_factory
from kalillac_routing.request_budget import RequestBudget, RequestBudgetError, RequestCancelled

from . import repository as repo
from .config import (
    MAX_BODY_BYTES,
    MESSAGE_PAGE_DEFAULT,
    MESSAGE_PAGE_MAX,
    PAGE_DEFAULT,
    PAGE_MAX,
    LEASE_GRACE_SECONDS,
    SpacesConfig,
)
from .context import WorkspaceContext, render_workspace_block
from .schemas import (
    CreateConversation,
    CreateSpace,
    RenameConversation,
    RenameSpace,
    SendMessage,
    SetInstructions,
    error_code,
)

ASSISTANT_MESSAGE_MAX = 24000

# Per-transaction PostgreSQL bounds (the quota transactions' pattern): a
# statement or lock wait never outlasts this, nor the send's remaining budget.
STATEMENT_TIMEOUT_CAP_SECONDS = 3.0
# Cleanup (releasing a lease) gets its own small allowance, separate from the
# send budget, so it can neither hang nor be skipped because the send expired.
CLEANUP_SECONDS = 2.0

_HEADERS = {"Cache-Control": "no-store", "Vary": "Cookie"}


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code}, headers=dict(_HEADERS))


def _json(content: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content=content, headers=dict(_HEADERS))


def _no_content() -> Response:
    return Response(status_code=204, headers=dict(_HEADERS))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
    return parsed if str(parsed) == value.lower() else None


class _Failure(Exception):
    def __init__(self, response: Response):
        super().__init__("failure")
        self.response = response


# A send's generation: runs the existing chat lifecycle (admission, provider
# controls, account quota) under the send's own budget and calls
# finalize(reply, settlement) to save. It returns the response to send.
# settlement, when given, has lock(session) and charge(session): the quota
# charge is written in the same transaction as the saved exchange.
Generate = Callable[..., Awaitable[Response]]


class SendRefused(Exception):
    """Raised by a settlement to refuse saving. status and code are public."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass
class _InFlight:
    """Budgets of Space sends that are generating, so a delete can cancel
    them (best effort: a provider may keep processing)."""

    lock: threading.Lock
    entries: dict

    def add(self, space_id, conversation_id, budget):
        with self.lock:
            self.entries.setdefault((space_id, conversation_id), set()).add(budget)

    def remove(self, space_id, conversation_id, budget):
        with self.lock:
            budgets = self.entries.get((space_id, conversation_id))
            if budgets is not None:
                budgets.discard(budget)
                if not budgets:
                    del self.entries[(space_id, conversation_id)]

    def cancel(self, space_id, conversation_id=None):
        with self.lock:
            budgets = [b for (sid, cid), group in self.entries.items()
                       if sid == space_id and (conversation_id is None or cid == conversation_id)
                       for b in group]
        for budget in budgets:
            budget.cancel()


def bound_phase(session: Session, budget: RequestBudget | None, cap: float) -> None:
    """Before a blocking database phase.

    With a budget: raise if it is spent or cancelled; otherwise bound each
    PostgreSQL wait by what remains of it at that moment (capped at `cap`),
    and check the budget again after each blocking SET LOCAL, so a
    cancellation or expiry that happens during one is never missed:

        check + remaining -> SET LOCAL lock_timeout      -> check
                + remaining -> SET LOCAL statement_timeout -> check

    The final check is the last thing this function does. Without a budget
    (cleanup) only the cap applies. SQLite has no per-statement timeouts,
    so there only the first check runs."""
    seconds = budget.select_call_timeout(cap)[0] if budget is not None else cap
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(text(f"SET LOCAL lock_timeout = '{max(1, math.ceil(seconds * 1000))}ms'"))
    if budget is not None:
        budget.ensure_open()
        seconds = budget.select_call_timeout(cap)[0]
    session.execute(text(f"SET LOCAL statement_timeout = '{max(1, math.ceil(seconds * 1000))}ms'"))
    if budget is not None:
        budget.ensure_open()


def run_transaction(session_factory: Callable[[], Any], work: Callable[..., Any], budget: RequestBudget | None,
                    cap: float, phased: bool = False) -> Any:
    """One Spaces transaction with an explicit, auditable commit boundary.

        [check] -> begin -> (PostgreSQL: READ COMMITTED) -> bound_phase
        -> work (phased work calls bound_phase between its phases)
        -> bound_phase  (its last operation is the final budget check)
        -> COMMIT

    Nothing runs between the final budget check and the commit call. Any
    exception before the commit -- including a cancellation or expiry seen
    by that final check -- rolls the whole transaction back: a saved
    exchange and its quota charge are durable together or not at all. A
    cancellation that arrives once the commit has begun cannot be honoured;
    that outcome is uncertain to the client and is reconciled by the
    request's idempotency key. Connection acquisition is bounded by the
    engine (pool_timeout, connect_timeout)."""
    if budget is not None:
        budget.ensure_open()
    factory = session_factory()
    if factory is None:
        raise RuntimeError("database disabled")
    with factory() as session:
        transaction = session.begin()
        try:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            bound_phase(session, budget, cap)
            if phased:
                result = work(session, lambda phase: bound_phase(session, budget, cap))
            else:
                result = work(session)
            bound_phase(session, budget, cap)        # ends with the final budget check
            transaction.commit()                      # nothing in between
            return result
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            raise


def build_spaces_router(
    *,
    config: SpacesConfig,
    cookie_name: str,
    generate: Generate,
    lease_seconds: int,
    session_factory: Callable[[], Any] = get_session_factory,
    clock: Callable[[], datetime] = utc_now,
    new_budget: Callable[[], RequestBudget] | None = None,
    cleanup_seconds: float = CLEANUP_SECONDS,
) -> APIRouter:
    router = APIRouter(prefix="/api/spaces")
    inflight = _InFlight(threading.Lock(), {})
    if new_budget is None:
        def new_budget() -> RequestBudget:
            return RequestBudget(duration_seconds=max(1, lease_seconds - LEASE_GRACE_SECONDS),
                                 max_model_attempts=6, max_search_attempts=6)

    async def run(work: Callable[[Session], Any], budget: RequestBudget | None = None,
                  cap: float = STATEMENT_TIMEOUT_CAP_SECONDS, phased: bool = False) -> Any:
        try:
            return await run_in_threadpool(run_transaction, session_factory, work, budget, cap, phased)
        except (repo.NotFound, repo.VersionConflict, repo.LimitReached, repo.Busy, repo.Full,
                repo.IdempotencyConflict, repo.InvalidCursor, _Failure, SendRefused, RequestBudgetError):
            raise
        except Exception:
            print("SPACES_DATABASE_UNAVAILABLE")
            raise _Failure(_error(503, "database_unavailable")) from None

    def access(auth: AuthContext | None) -> Response | None:
        if auth is None:
            return _error(401, "not_authenticated")
        if not config.has_beta_access(auth.user_id):
            return _error(404, "not_found")
        if not auth.email_verified:
            return _error(403, "email_verification_required")
        return None

    async def authorize(request: Request, budget: RequestBudget | None = None) -> AuthContext:
        token_hash = session_token_hash(request.cookies, cookie_name)
        if token_hash is None:
            raise _Failure(_error(401, "not_authenticated"))
        auth = await run(lambda session: resolve_account(session, token_hash, clock()), budget)
        denied = access(auth)
        if denied is not None:
            raise _Failure(denied)
        return auth

    async def body(request: Request, model: type[BaseModel]) -> BaseModel:
        try:
            require_same_origin(request.headers, config.site_origin)
            raw = await read_json_object(request, MAX_BODY_BYTES)
        except RequestRejected as rejected:
            raise _Failure(_error(rejected.status, rejected.code)) from None
        try:
            return model.model_validate(raw)
        except ValidationError as invalid:
            raise _Failure(_error(422, error_code(invalid.errors()))) from None

    def same_origin(request: Request) -> None:
        try:
            require_same_origin(request.headers, config.site_origin)
        except RequestRejected as rejected:
            raise _Failure(_error(rejected.status, rejected.code)) from None

    def query(request: Request, allowed: set[str]) -> dict[str, str]:
        params = request.query_params
        if set(params.keys()) - allowed or any(len(params.getlist(k)) > 1 for k in params.keys()):
            raise _Failure(_error(422, "invalid_request"))
        return {k: params[k] for k in params.keys()}

    def bounded_int(value: str | None, default: int, low: int, high: int) -> int:
        if value is None:
            return default
        # ASCII digits only: str.isdigit() also accepts superscripts and other
        # scripts' digits, which int() then rejects.
        if not (value.isascii() and value.isdigit()) or len(value) > 6 or not low <= int(value) <= high:
            raise _Failure(_error(422, "invalid_request"))
        return int(value)

    def ids(*values: str) -> list[uuid.UUID]:
        parsed = [_parse_id(v) for v in values]
        if any(p is None for p in parsed):
            raise _Failure(_error(404, "not_found"))
        return parsed

    def mapped(exc: Exception) -> Response:
        if isinstance(exc, repo.NotFound):
            return _error(404, "not_found")
        if isinstance(exc, repo.VersionConflict):
            return _error(409, "version_conflict")
        if isinstance(exc, repo.LimitReached):
            return _error(409, exc.code)
        if isinstance(exc, repo.Busy):
            return _error(409, "conversation_busy")
        if isinstance(exc, repo.Full):
            return _error(409, "conversation_full")
        if isinstance(exc, repo.IdempotencyConflict):
            return _error(409, "idempotency_conflict")
        if isinstance(exc, repo.InvalidCursor):
            return _error(422, "invalid_request")
        if isinstance(exc, _Failure):
            return exc.response
        if isinstance(exc, SendRefused):
            return _error(exc.status, exc.code)
        if isinstance(exc, RequestCancelled):
            return _error(499, "request_cancelled")
        if isinstance(exc, RequestBudgetError):
            return _error(504, "request_timeout")
        raise exc

    def handler(function):
        # wraps() keeps the endpoint's signature visible to FastAPI, so path
        # parameters are still bound by name.
        @functools.wraps(function)
        async def wrapped(request: Request, **path):
            try:
                return await function(request, **path)
            except _Failure as failure:
                return failure.response
            except (repo.NotFound, repo.VersionConflict, repo.LimitReached, repo.Busy, repo.Full,
                    repo.IdempotencyConflict, repo.InvalidCursor, SendRefused, RequestBudgetError) as known:
                return mapped(known)
        return wrapped

    # --- Spaces ---------------------------------------------------------------------

    @router.get("")
    @handler
    async def list_spaces(request: Request):
        auth = await authorize(request)
        q = query(request, {"cursor", "limit"})
        limit = bounded_int(q.get("limit"), PAGE_DEFAULT, 1, PAGE_MAX)
        spaces, next_cursor = await run(
            lambda s: repo.list_spaces(s, auth.user_id, limit, q.get("cursor")))
        return _json({"spaces": spaces, "next_cursor": next_cursor})

    @router.post("")
    @handler
    async def create_space(request: Request):
        auth = await authorize(request)
        query(request, set())
        data = await body(request, CreateSpace)
        space = await run(lambda s: repo.space_json(
            repo.create_space(s, auth.user_id, data.name, data.instructions, clock())))
        return _json({"space": space}, 201)

    @router.get("/{space_id}")
    @handler
    async def read_space(request: Request, space_id: str):
        auth = await authorize(request)
        query(request, set())
        (sid,) = ids(space_id)
        space = await run(lambda s: repo.space_json(repo.get_space(s, auth.user_id, sid)))
        return _json({"space": space})

    @router.patch("/{space_id}")
    @handler
    async def rename_space(request: Request, space_id: str):
        auth = await authorize(request)
        query(request, set())
        data = await body(request, RenameSpace)
        (sid,) = ids(space_id)
        space = await run(lambda s: repo.space_json(
            repo.rename_space(s, auth.user_id, sid, data.name, data.expected_version, clock())))
        return _json({"space": space})

    @router.put("/{space_id}/instructions")
    @handler
    async def set_instructions(request: Request, space_id: str):
        auth = await authorize(request)
        query(request, set())
        data = await body(request, SetInstructions)
        (sid,) = ids(space_id)
        space = await run(lambda s: repo.space_json(
            repo.set_instructions(s, auth.user_id, sid, data.instructions, data.expected_version, clock())))
        return _json({"space": space})

    @router.delete("/{space_id}")
    @handler
    async def delete_space(request: Request, space_id: str):
        auth = await authorize(request)
        query(request, set())
        same_origin(request)
        (sid,) = ids(space_id)
        await run(lambda s: repo.delete_space(s, auth.user_id, sid))
        inflight.cancel(sid)
        return _no_content()

    # --- conversations -------------------------------------------------------------

    @router.get("/{space_id}/conversations")
    @handler
    async def list_conversations(request: Request, space_id: str):
        auth = await authorize(request)
        q = query(request, {"cursor", "limit"})
        (sid,) = ids(space_id)
        limit = bounded_int(q.get("limit"), PAGE_DEFAULT, 1, PAGE_MAX)
        conversations, next_cursor = await run(
            lambda s: repo.list_conversations(s, auth.user_id, sid, limit, q.get("cursor")))
        return _json({"conversations": conversations, "next_cursor": next_cursor})

    @router.post("/{space_id}/conversations")
    @handler
    async def create_conversation(request: Request, space_id: str):
        auth = await authorize(request)
        query(request, set())
        data = await body(request, CreateConversation)
        (sid,) = ids(space_id)
        conversation = await run(lambda s: repo.conversation_json(
            repo.create_conversation(s, auth.user_id, sid, data.title, clock())))
        return _json({"conversation": conversation}, 201)

    @router.get("/{space_id}/conversations/{conversation_id}")
    @handler
    async def read_conversation(request: Request, space_id: str, conversation_id: str):
        auth = await authorize(request)
        q = query(request, {"before_seq", "limit"})
        sid, cid = ids(space_id, conversation_id)
        limit = bounded_int(q.get("limit"), MESSAGE_PAGE_DEFAULT, 1, MESSAGE_PAGE_MAX)
        before = None if q.get("before_seq") is None else bounded_int(q.get("before_seq"), 0, 1, 10**6)

        def work(s):
            conversation, rows, next_before = repo.get_conversation_page(s, auth.user_id, sid, cid, before, limit)
            return {
                "conversation": repo.conversation_json(conversation),
                "messages": [repo.message_json(m) for m in rows],
                "next_before_seq": next_before,
            }

        return _json(await run(work))

    @router.patch("/{space_id}/conversations/{conversation_id}")
    @handler
    async def rename_conversation(request: Request, space_id: str, conversation_id: str):
        auth = await authorize(request)
        query(request, set())
        data = await body(request, RenameConversation)
        sid, cid = ids(space_id, conversation_id)
        conversation = await run(lambda s: repo.conversation_json(repo.rename_conversation(
            s, auth.user_id, sid, cid, data.title, data.expected_version, clock())))
        return _json({"conversation": conversation})

    @router.delete("/{space_id}/conversations/{conversation_id}")
    @handler
    async def delete_conversation(request: Request, space_id: str, conversation_id: str):
        auth = await authorize(request)
        query(request, set())
        same_origin(request)
        sid, cid = ids(space_id, conversation_id)
        await run(lambda s: repo.delete_conversation(s, auth.user_id, sid, cid))
        inflight.cancel(sid, cid)
        return _no_content()

    # --- persistent send -----------------------------------------------------------

    @router.post("/{space_id}/conversations/{conversation_id}/messages")
    @handler
    async def send(request: Request, space_id: str, conversation_id: str):
        # One budget bounds the whole send: authorization, preparation,
        # generation, the recheck and persistence. Only cleanup has its own,
        # separate allowance.
        budget = new_budget()
        auth = await authorize(request, budget)
        query(request, set())
        data = await body(request, SendMessage)
        sid, cid = ids(space_id, conversation_id)
        request_id = uuid.UUID(data.client_request_id)
        message = data.message
        # Registered before any database work, so deleting the conversation
        # or Space cancels this send at any stage.
        inflight.add(sid, cid, budget)
        try:
            return await send_within(auth, sid, cid, request_id, message, budget, request)
        except BaseException:
            # The handler stopped (cancelled, disconnected or failed): any
            # database work still running in a worker sees the cancelled
            # budget at its next check and rolls back instead of committing.
            budget.cancel()
            raise
        finally:
            inflight.remove(sid, cid, budget)

    async def send_within(auth, sid, cid, request_id, message, budget, request) -> Response:
        # Transaction A: replay, refuse, or take a lease under a fresh
        # server attempt token, and read the context. The budget is checked
        # again once the conversation row is locked.
        prepared = await run(lambda s: repo.begin_send(
            s, auth.user_id, sid, cid, request_id, message, clock(), lease_seconds, check=budget.ensure_open), budget)
        if isinstance(prepared, repo.Replay):
            return _replay(prepared)
        attempt = prepared.attempt

        async def release() -> Response | None:
            # Clears the lease only if this attempt still holds it. Bounded by
            # its own allowance; a lease that cannot be cleared expires.
            try:
                exists = await asyncio.wait_for(
                    run(lambda s: repo.release_lease(s, auth.user_id, sid, cid, attempt), None, cleanup_seconds),
                    timeout=cleanup_seconds)
            except asyncio.TimeoutError:
                print("SPACES_CLEANUP_TIMEOUT")
                return None
            except _Failure:
                return None
            return None if exists else _error(404, "not_found")

        async def finalize(reply: str, settlement: Any = None) -> Response:
            # Transaction B. Under the account and conversation locks:
            # this attempt must still hold the lease; the session, beta
            # access, verification and ownership are rechecked; the budget
            # must still be open; then the quota charge and both messages
            # are written together -- or nothing is.
            if len(reply) > ASSISTANT_MESSAGE_MAX:
                return (await release()) or _error(422, "processing_limit_reached")

            def work(s, phase_bound):
                def verify():
                    # The live session, account activity, verification, beta
                    # access and ownership, read after the locks are held.
                    current = resolve_account(s, auth.token_hash, clock())
                    if current is not None and current.user_id != auth.user_id:
                        current = None
                    denied = access(current)
                    if denied is not None:
                        raise _Failure(denied)

                return repo.finish_send(
                    s, auth.user_id, sid, cid, attempt, request_id, message, reply, clock(),
                    bound=phase_bound,
                    lock_account=(lambda: settlement.lock(s)) if settlement is not None else repo._no_op,
                    verify=verify,
                    settle=(lambda: settlement.charge(s)) if settlement is not None else repo._no_op,
                )

            try:
                outcome = await run(work, budget, phased=True)
            except repo.NotFound:
                return _error(404, "not_found")          # deleted: nothing to release
            except (_Failure, SendRefused, RequestBudgetError, repo.Busy, repo.Full) as stopped:
                return (await release()) or mapped(stopped)
            except BaseException:
                budget.cancel()        # a cancelled handler stops the worker's transaction too
                raise
            return _replay(outcome)

        workspace = WorkspaceContext(prompt_block=render_workspace_block(prepared.instructions))
        try:
            response = await generate(
                request,
                user_id=auth.user_id,
                lock_key=f"space:{cid}",
                message=message,
                history=prepared.history,
                workspace=workspace,
                budget=budget,
                finalize=finalize,
            )
        except BaseException:
            await release()
            raise

        if response.status_code != 200:
            # Generation failed or was refused: nothing was saved.
            return (await release()) or _with_headers(response)
        return response

    return router


def _replay(result: repo.Replay) -> JSONResponse:
    return _json({
        "user_message": result.user_message,
        "assistant_message": result.assistant_message,
        "conversation": result.conversation,
    })


def _with_headers(response: Response) -> Response:
    for name, value in _HEADERS.items():
        response.headers[name] = value
    return response
