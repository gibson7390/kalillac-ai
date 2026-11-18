"""/api/files: upload, list, download and delete a customer's files, and ask the file assistant.

Installed only when KALILLAC_FILES_ENABLED is set (with accounts and the database). Every route needs
a signed-in account with the tier the configuration requires (KALILLAC_FILES_TIER, "paid" unless set
otherwise; the Free/Premium split is an owner decision and stays fail-closed until made). Unsafe
routes are in the unsafe-request guard's inventory, so a cross-site request is refused before it
gets here.

    POST   /api/files              upload: the raw file as the body (application/octet-stream), its
                                   name percent-encoded in X-Kalillac-Filename; checked by intake in
                                   the isolated worker before anything is stored
    GET    /api/files              the account's files
    GET    /api/files/{file_id}    download (attachment, never rendered by the browser)
    DELETE /api/files/{file_id}
    POST   /api/files/assist       {"message", "history"?, "file_ids"?, "confirmations"?, "reasoning_mode"?}:
                                   the file assistant makes or edits files; the reply lists them

Responses never contain a path, an owner id or document text beyond what the customer asked for;
errors are stable codes.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import json
import secrets
import threading
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import images
from .intake import MAX_UPLOAD_BYTES, display_name
from .store import FileNotFound, QuotaExceeded

MEDIA_BY_KIND = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8", "csv": "text/csv; charset=utf-8",
    "json": "application/json",
}
MAX_MESSAGE = 20_000
MAX_HISTORY = 20
MAX_ATTACHED = 5
_NO_STORE = {"Cache-Control": "no-store", "Vary": "Cookie"}


class RequestRefused(Exception):
    """A check the application supplies (the reasoning mode) refused the request before any work."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


@dataclass(frozen=True)
class FileAccount:
    owner: str          # a stable account id
    paid: bool
    # V1 mode: the V1-authorized account with its effective entitlement (kalillac_accounts.paid_access),
    # from which the file request's PAID evidence is issued. None on the legacy path: never paid scope.
    verified: object = None


def _error(status, code):
    return JSONResponse(status_code=status, content={"error": code}, headers=_NO_STORE)


async def _bounded_body(request, limit):
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


MAX_CONFIRMATIONS = 4
CONFIRMATION_SECONDS = 30 * 60
CONFIRMATIONS_PER_OWNER = 20


def valid_confirmations(confirmations):
    """Exact-edit confirmations a client presents: a short list of the service's own ids."""
    return (type(confirmations) is list and len(confirmations) <= MAX_CONFIRMATIONS
            and all(type(item) is str and 0 < len(item) <= 64 for item in confirmations))


def valid_assist_request(message, history, file_ids):
    """Whether a file-assistant request is well formed (the same rules for /api/files/assist and /api/chat)."""
    return (type(message) is str and bool(message.strip()) and len(message) <= MAX_MESSAGE
            and type(history) is list and len(history) <= MAX_HISTORY
            and all(type(turn) is dict and set(turn) == {"role", "content"} and turn["role"] in ("user", "assistant")
                    and type(turn["content"]) is str and len(turn["content"]) <= MAX_MESSAGE for turn in history)
            and type(file_ids) is list and len(file_ids) <= MAX_ATTACHED
            and all(type(file_id) is str for file_id in file_ids))


class FileService:
    """Access checks and the file assistant, shared by /api/files and the /api/chat handoff."""

    def __init__(self, *, store, authenticate: Callable[[Request], Awaitable[FileAccount | None]],
                 call_model: Callable[[list], Any], required_tier: str = "paid", max_concurrent_assists: int = 2,
                 worker_module=None, request_scope: Callable[[FileAccount], Any] | None = None,
                 failure_status: Callable[[BaseException], tuple[int, str] | None] | None = None,
                 images: bool = False,
                 count_response: Callable[..., Awaitable[Any]] | None = None,
                 reasoning: Callable[[FileAccount, object], Callable[[], Any]] | None = None):
        """request_scope(account) is a context manager entered in the worker thread around one assistant
        run (the application's request budget and provider controls); failure_status maps an exception
        those controls raise to (status, stable code). images=True accepts image uploads (I1): each is
        re-encoded clean by the worker (kalillac_files.images) and only that copy is stored.
        count_response(request, account, request_chars, produce), when given, runs one assistant
        response (produce()) under the customer's daily response allowance: admitted before any model
        work, counted once when it succeeds, released when it fails.
        reasoning(account, requested mode) checks /api/files/assist's reasoning_mode before any work and
        returns a context-manager factory that binds it in the worker thread, or raises RequestRefused;
        None while reasoning modes are off (the value is then ignored, as in the chat)."""
        if required_tier not in ("paid", "any"):
            raise ValueError("required_tier must be 'paid' or 'any'")
        if type(images) is not bool:
            raise ValueError("images must be a bool")
        self.images = images
        self.count_response = count_response
        self.reasoning = reasoning
        from . import worker as default_worker
        self.store, self.authenticate, self.call_model = store, authenticate, call_model
        self.request_scope = request_scope or (lambda account: nullcontext())
        self.failure_status = failure_status or (lambda error: None)
        self.required_tier = required_tier
        self.worker = worker_module or default_worker
        self.assists = threading.BoundedSemaphore(max_concurrent_assists)
        # Exact-edit confirmations the service issued: id -> (owner, file id, edit digest, expiry).
        self.confirmations, self.confirmations_lock = {}, threading.Lock()
        self.clock = time.monotonic

    def _offer(self, owner):
        def offer(file_id, digest):
            with self.confirmations_lock:
                now = self.clock()
                for key in [k for k, v in self.confirmations.items() if v[3] <= now]:
                    del self.confirmations[key]
                mine = sorted((v[3], k) for k, v in self.confirmations.items() if v[0] == owner)
                for _expiry, key in mine[:max(0, len(mine) - CONFIRMATIONS_PER_OWNER + 1)]:
                    del self.confirmations[key]
                key = secrets.token_urlsafe(18)
                self.confirmations[key] = (owner, file_id, digest, now + CONFIRMATION_SECONDS)
                return key
        return offer

    def _claimer(self, owner, presented):
        """claim(file id, digest): atomically consume one confirmation this owner presented with this
        request that is bound to exactly that file and edit digest (operations and tracked mode) and not
        expired. Under the lock, so two requests presenting the same confirmation cannot both claim it;
        a claimed confirmation is gone before the edit runs and is never restored."""
        presented = tuple(dict.fromkeys(presented or ()))

        def claim(file_id, digest):
            with self.confirmations_lock:
                now = self.clock()
                for key in presented:
                    value = self.confirmations.get(key)
                    if (value is not None and value[0] == owner and value[1] == file_id and value[2] == digest
                            and value[3] > now):
                        del self.confirmations[key]
                        return True
                return False
        return claim

    async def account(self, request):
        """(account, None) when the caller may use files, else (None, the refusal response)."""
        found = await self.authenticate(request)
        if found is None:
            return None, _error(401, "not_authenticated")
        if self.required_tier == "paid" and not found.paid:
            return None, _error(403, "files_require_premium")
        return found, None

    async def assist(self, account, message, history, file_ids, confirmations=(), *, scope=None, stopped=None):
        """Run the file assistant: ({"reply", "files"[, "confirmations"]}, None) or (None, the refusal
        response). confirmations: exact-edit confirmation ids the customer confirmed in the product.
        scope(): a context manager entered in the worker thread inside request_scope (the request's
        reasoning mode and its chat session's cancellation). stopped(): True once the request's chat
        session has ended; the assistant then does nothing more."""
        from .assistant import AssistantError, AssistantStopped, FileRequest, run_file_assistant
        for file_id in file_ids:
            try:
                await run_in_threadpool(self.store.record, account.owner, file_id)
            except FileNotFound:
                return None, _error(404, "file_not_found")
        if not self.assists.acquire(blocking=False):
            return None, _error(503, "busy")
        try:
            file_request = FileRequest(account.owner, self.store, tuple(dict.fromkeys(file_ids)),
                                       run_generate=self.worker.run_generate, run_outline=self.worker.run_outline,
                                       run_edit=self.worker.run_edit,
                                       claim=self._claimer(account.owner, confirmations),
                                       offer=self._offer(account.owner), stopped=stopped)
            def run():
                with self.request_scope(account), (scope or nullcontext)():
                    return run_file_assistant(message, history, file_request, self.call_model)
            result = await run_in_threadpool(run)
        except AssistantStopped:
            return None, _error(499, "request_cancelled")
        except AssistantError as failed:
            return None, _error(502, failed.code)
        except Exception as failed:  # The model provider, a limit or a defect: a class name only, never content.
            mapped = self.failure_status(failed)
            if mapped is not None:
                return None, _error(*mapped)
            print(f"WARN: FILE_ASSISTANT_FAILED {type(failed).__name__}")
            return None, _error(502, "file_assistant_unavailable")
        finally:
            self.assists.release()
        content = {"reply": result.text, "files": [record.public() for record in result.files]}
        if result.confirmations:
            content["confirmations"] = list(result.confirmations)
        return content, None


def build_files_router(*, service: FileService | None = None, **options) -> APIRouter:
    service = service or FileService(**options)
    store, worker, account = service.store, service.worker, service.account
    router = APIRouter(prefix="/api/files")

    def no_query(request):
        return _error(400, "query_not_allowed") if request.url.query else None

    @router.post("")
    async def upload(request: Request):
        refusal = no_query(request)
        if refusal:
            return refusal
        found, refusal = await account(request)
        if refusal:
            return refusal
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/octet-stream":
            return _error(415, "octet_stream_required")
        raw_name = request.headers.get("x-kalillac-filename", "")
        try:
            name = display_name(unquote(raw_name, errors="strict")) if raw_name and len(raw_name) <= 765 else None
        except UnicodeDecodeError:
            name = None
        if name is None:
            return _error(400, "filename_required")
        data = await _bounded_body(request, MAX_UPLOAD_BYTES)
        if data is None:
            return _error(413, "file_too_large")
        if not data:
            return _error(400, "empty_file")
        try:
            if service.images and (images.sniff(data) is not None or images.is_image_name(name)):
                # An image is decoded once in the worker and replaced by Kalillac's own clean copy;
                # the uploaded bytes are never stored.
                sanitized = await run_in_threadpool(worker.run_image, data, name)
                data, name, media_type = sanitized.data, images.output_name(name, sanitized.kind), \
                    images.MEDIA[sanitized.kind]
            else:
                inspected = await run_in_threadpool(worker.run_inspect, data, name)
                media_type = MEDIA_BY_KIND[inspected.kind]
        except worker.WorkerError as refused:
            return _error(422 if refused.code not in ("busy", "timeout", "worker_failed") else 503, refused.code)
        try:
            stored = await run_in_threadpool(store.put, found.owner, data, name, media_type, "uploaded")
        except QuotaExceeded as full:
            return _error(413 if full.code == "file_too_large" else 409, full.code)
        return JSONResponse(status_code=201, content={"file": stored.public()}, headers=_NO_STORE)

    @router.get("")
    async def list_files(request: Request):
        refusal = no_query(request)
        if refusal:
            return refusal
        found, refusal = await account(request)
        if refusal:
            return refusal
        records = await run_in_threadpool(store.list, found.owner)
        return JSONResponse(content={"files": [record.public() for record in records]}, headers=_NO_STORE)

    @router.get("/{file_id}")
    async def download(file_id: str, request: Request):
        refusal = no_query(request)
        if refusal:
            return refusal
        found, refusal = await account(request)
        if refusal:
            return refusal
        try:
            record, data = await run_in_threadpool(store.get, found.owner, file_id)
        except FileNotFound:
            return _error(404, "file_not_found")
        ascii_name = record.filename.encode("ascii", "replace").decode("ascii").replace('"', "_").replace("?", "_")
        return Response(content=data, media_type=record.media_type, headers={
            **_NO_STORE,
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(record.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
        })

    @router.delete("/{file_id}")
    async def delete(file_id: str, request: Request):
        refusal = no_query(request)
        if refusal:
            return refusal
        found, refusal = await account(request)
        if refusal:
            return refusal
        try:
            await run_in_threadpool(store.delete, found.owner, file_id)
        except FileNotFound:
            return _error(404, "file_not_found")
        return JSONResponse(content={"deleted": True}, headers=_NO_STORE)

    @router.post("/assist")
    async def assist(request: Request):
        refusal = no_query(request)
        if refusal:
            return refusal
        found, refusal = await account(request)
        if refusal:
            return refusal
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return _error(415, "json_required")
        body = await _bounded_body(request, 512 * 1024)
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not set(payload) <= {"message", "history", "file_ids", "confirmations",
                                                                 "reasoning_mode"}:
            return _error(422, "invalid_request")
        message, history, file_ids = payload.get("message"), payload.get("history", []), payload.get("file_ids", [])
        confirmations = payload.get("confirmations", [])
        if not valid_assist_request(message, history, file_ids) or not valid_confirmations(confirmations):
            return _error(422, "invalid_request")
        # The reasoning mode, refused here before the allowance or any model work, as in the chat.
        scope = None
        if service.reasoning is not None:
            try:
                scope = service.reasoning(found, payload.get("reasoning_mode"))
            except RequestRefused as refused:
                return _error(refused.status, refused.code)

        async def produce():
            content, refusal = await service.assist(found, message, history, file_ids, confirmations, scope=scope)
            if refusal:
                return refusal
            return JSONResponse(content=content, headers=_NO_STORE)
        if service.count_response is None:
            return await produce()
        # One response against the customer's daily allowance: admitted before the assistant runs.
        request_chars = len(message) + sum(len(turn["content"]) for turn in history)
        response = await service.count_response(request, found, request_chars, produce)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    return router
