"""Spaces data access.

Every function takes the caller's Session and the authenticated account id,
and every query filters by that account (and by space_id for children). A
row that does not exist and a row owned by another account are the same
NotFound. Functions never commit; the router owns each short transaction.

Global lock order (PostgreSQL row locks). Every transaction that holds more
than one of these takes them in this order and never the reverse:

    1. account   (users row: quota and account work, FOR NO KEY UPDATE)
    2. Space     (spaces row: lock_space, FOR NO KEY UPDATE; a DELETE)
    3. conversation (conversations row: lock_conversation, FOR UPDATE; a DELETE)
    4. messages  (inserted or cascade-deleted under their conversation)

  - finish_send (the save): account -> Space -> conversation -> messages.
  - delete_space: Space -> its conversations -> their messages (cascade).
  - delete_conversation: conversation -> its messages (cascade).
  - account deletion (cascade): account -> Spaces -> conversations -> messages.
  - create_space: account; create_conversation: Space.
  - begin_send and release_lease: the conversation only (the Space and the
    account are read, never locked).

So no path holds a conversation while waiting for its Space or account, and
no path holds a Space while waiting for an account.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any
import uuid

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from kalillac_db.models import Conversation, Message, Space
from kalillac_db.repositories.billing import lock_account

from .config import (
    DEFAULT_CONVERSATION_TITLE,
    HISTORY_CHARS_MAX,
    HISTORY_MESSAGES_MAX,
    MAX_CONVERSATIONS_PER_SPACE,
    MAX_MESSAGES_PER_CONVERSATION,
    MAX_SPACES_PER_ACCOUNT,
)


class NotFound(Exception):
    pass


class VersionConflict(Exception):
    pass


class LimitReached(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Busy(Exception):
    pass


class InvalidAttempt(Busy):
    """An internal lease token that is missing or not a UUID. Fails closed,
    exactly like a stale token."""


class Full(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


class InvalidCursor(Exception):
    pass


def as_utc(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; PostgreSQL returns aware ones.
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return None if value is None else value.isoformat()


# --- cursors -----------------------------------------------------------------------
# Keyset pagination over (updated_at DESC, id DESC). The cursor is an opaque
# encoding of the last row's key; it carries no authority, so it is not
# signed, and it is always applied together with the account filter.

def encode_cursor(updated_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([iso(updated_at), str(row_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, row_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            raise ValueError
        return moment, uuid.UUID(row_id)
    except Exception:
        raise InvalidCursor() from None


def _keyset(model, cursor: str | None):
    if cursor is None:
        return None
    moment, row_id = decode_cursor(cursor)
    return or_(model.updated_at < moment, and_(model.updated_at == moment, model.id < row_id))


def _page(rows: list, limit: int) -> tuple[list, str | None]:
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        last = last[0] if isinstance(last, tuple) or hasattr(last, "_mapping") else last
        return rows, encode_cursor(last.updated_at, last.id)
    return rows, None


# --- Spaces ------------------------------------------------------------------------

def space_json(space: Space) -> dict[str, Any]:
    return {
        "id": str(space.id),
        "name": space.name,
        "instructions": space.instructions,
        "instructions_updated_at": iso(space.instructions_updated_at),
        "version": space.version,
        "created_at": iso(space.created_at),
        "updated_at": iso(space.updated_at),
    }


def _owned_space(session: Session, user_id: uuid.UUID, space_id: uuid.UUID, *, lock: bool = False) -> Space:
    statement = select(Space).where(Space.id == space_id, Space.user_id == user_id)
    if lock:
        statement = statement.with_for_update()
    space = session.scalar(statement)
    if space is None:
        raise NotFound()
    return space


def create_space(session: Session, user_id: uuid.UUID, name: str, instructions: str, now: datetime) -> Space:
    # The account lock serializes concurrent creates, so the count is exact.
    if not lock_account(session, user_id):
        raise NotFound()
    count = session.scalar(select(func.count()).select_from(Space).where(Space.user_id == user_id))
    if count >= MAX_SPACES_PER_ACCOUNT:
        raise LimitReached("space_limit_reached")
    space = Space(
        user_id=user_id, name=name, instructions=instructions, version=1,
        created_at=now, updated_at=now, instructions_updated_at=now if instructions else None,
    )
    session.add(space)
    session.flush()
    return space


def list_spaces(session: Session, user_id: uuid.UUID, limit: int, cursor: str | None):
    conversations = (
        select(func.count()).select_from(Conversation)
        .where(Conversation.space_id == Space.id, Conversation.user_id == user_id)
        .scalar_subquery()
    )
    statement = select(Space, conversations).where(Space.user_id == user_id)
    keyset = _keyset(Space, cursor)
    if keyset is not None:
        statement = statement.where(keyset)
    rows = session.execute(statement.order_by(Space.updated_at.desc(), Space.id.desc()).limit(limit + 1)).all()
    page, next_cursor = _page(rows, limit)
    return [
        {"id": str(space.id), "name": space.name, "conversation_count": count, "updated_at": iso(space.updated_at)}
        for space, count in page
    ], next_cursor


def get_space(session: Session, user_id: uuid.UUID, space_id: uuid.UUID) -> Space:
    return _owned_space(session, user_id, space_id)


def _versioned_update(session, model, filters, expected_version: int, values: dict) -> None:
    result = session.execute(
        update(model).where(*filters, model.version == expected_version)
        .values(version=model.version + 1, **values)
    )
    if result.rowcount == 1:
        return
    if session.scalar(select(func.count()).select_from(model).where(*filters)) == 0:
        raise NotFound()
    raise VersionConflict()


def rename_space(session, user_id, space_id, name, expected_version, now) -> Space:
    filters = (Space.id == space_id, Space.user_id == user_id)
    _versioned_update(session, Space, filters, expected_version, {"name": name, "updated_at": now})
    session.expire_all()
    return _owned_space(session, user_id, space_id)


def set_instructions(session, user_id, space_id, instructions, expected_version, now) -> Space:
    filters = (Space.id == space_id, Space.user_id == user_id)
    _versioned_update(session, Space, filters, expected_version, {
        "instructions": instructions, "instructions_updated_at": now, "updated_at": now,
    })
    session.expire_all()
    return _owned_space(session, user_id, space_id)


def delete_space(session, user_id, space_id) -> None:
    # Its conversations and messages go with it (ON DELETE CASCADE).
    result = session.execute(delete(Space).where(Space.id == space_id, Space.user_id == user_id))
    if result.rowcount != 1:
        raise NotFound()


# --- conversations ---------------------------------------------------------------

def conversation_json(conversation: Conversation) -> dict[str, Any]:
    return {
        "id": str(conversation.id),
        "space_id": str(conversation.space_id),
        "title": conversation.title,
        "message_count": conversation.message_count,
        "last_message_at": iso(conversation.last_message_at),
        "version": conversation.version,
        "created_at": iso(conversation.created_at),
        "updated_at": iso(conversation.updated_at),
    }


def conversation_summary_json(conversation: Conversation) -> dict[str, Any]:
    return {
        "id": str(conversation.id),
        "title": conversation.title,
        "message_count": conversation.message_count,
        "last_message_at": iso(conversation.last_message_at),
        "updated_at": iso(conversation.updated_at),
    }


def message_json(message: Message) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "seq": message.seq,
        "role": message.role,
        "content": message.content,
        "created_at": iso(message.created_at),
    }


def _conversation_filters(user_id, space_id, conversation_id):
    return (
        Conversation.id == conversation_id,
        Conversation.space_id == space_id,
        Conversation.user_id == user_id,
    )


def _owned_conversation(session, user_id, space_id, conversation_id, *, lock=False) -> Conversation:
    statement = select(Conversation).where(*_conversation_filters(user_id, space_id, conversation_id))
    if lock:
        statement = statement.with_for_update()
    conversation = session.scalar(statement)
    if conversation is None:
        raise NotFound()
    return conversation


def create_conversation(session, user_id, space_id, title, now) -> Conversation:
    space = _owned_space(session, user_id, space_id, lock=True)
    count = session.scalar(
        select(func.count()).select_from(Conversation)
        .where(Conversation.space_id == space.id, Conversation.user_id == user_id)
    )
    if count >= MAX_CONVERSATIONS_PER_SPACE:
        raise LimitReached("conversation_limit_reached")
    conversation = Conversation(
        space_id=space.id, user_id=user_id, title=title or DEFAULT_CONVERSATION_TITLE,
        message_count=0, version=1, created_at=now, updated_at=now,
    )
    session.add(conversation)
    space.updated_at = now
    session.flush()
    return conversation


def list_conversations(session, user_id, space_id, limit, cursor):
    _owned_space(session, user_id, space_id)
    statement = select(Conversation).where(Conversation.space_id == space_id, Conversation.user_id == user_id)
    keyset = _keyset(Conversation, cursor)
    if keyset is not None:
        statement = statement.where(keyset)
    rows = list(session.scalars(
        statement.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit + 1)
    ))
    page, next_cursor = _page(rows, limit)
    return [conversation_summary_json(c) for c in page], next_cursor


def get_conversation_page(session, user_id, space_id, conversation_id, before_seq, limit):
    """The conversation and one page of messages, newest page first; the page
    itself is in chronological order."""
    conversation = _owned_conversation(session, user_id, space_id, conversation_id)
    statement = select(Message).where(
        Message.conversation_id == conversation.id, Message.user_id == user_id,
    )
    if before_seq is not None:
        statement = statement.where(Message.seq < before_seq)
    rows = list(session.scalars(statement.order_by(Message.seq.desc()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    next_before = rows[0].seq if more and rows else None
    return conversation, rows, next_before


def rename_conversation(session, user_id, space_id, conversation_id, title, expected_version, now) -> Conversation:
    filters = _conversation_filters(user_id, space_id, conversation_id)
    _versioned_update(session, Conversation, filters, expected_version, {"title": title, "updated_at": now})
    session.expire_all()
    return _owned_conversation(session, user_id, space_id, conversation_id)


def delete_conversation(session, user_id, space_id, conversation_id) -> None:
    result = session.execute(delete(Conversation).where(*_conversation_filters(user_id, space_id, conversation_id)))
    if result.rowcount != 1:
        raise NotFound()


# --- the send lease ----------------------------------------------------------------

@dataclass(frozen=True)
class Replay:
    """The stored exchange for an already completed idempotency key."""
    user_message: dict
    assistant_message: dict
    conversation: dict


@dataclass(frozen=True)
class Prepared:
    """A send that now holds the conversation's lease.

    attempt is the server-generated lease token. It is distinct from the
    client's idempotency key: a retry with the same key gets a new attempt,
    so a stale attempt can neither complete nor release the new lease."""
    instructions: str
    history: list[dict[str, str]]
    attempt: uuid.UUID


def history_window(session, user_id, conversation_id) -> list[dict[str, str]]:
    """This conversation's completed exchanges only: newest whole exchanges
    first, within HISTORY_MESSAGES_MAX and HISTORY_CHARS_MAX, then restored
    to chronological order. A message is never split. Deterministic for a
    given database state."""
    rows = list(session.scalars(
        select(Message).where(Message.conversation_id == conversation_id, Message.user_id == user_id)
        .order_by(Message.seq.desc()).limit(HISTORY_MESSAGES_MAX)
    ))
    rows.reverse()
    # Stored in exchanges (user seq odd, assistant seq even); keep whole pairs.
    if rows and rows[0].role != "user":
        rows = rows[1:]
    pairs = [rows[i:i + 2] for i in range(0, len(rows) - 1, 2)]
    kept, total = [], 0
    for pair in reversed(pairs):
        size = sum(len(m.content) for m in pair)
        if kept and total + size > HISTORY_CHARS_MAX:
            break
        kept.append(pair)
        total += size
    kept.reverse()
    # Each record keeps its identity (id, seq) and its exact stored content;
    # consumers select records by identity or position, never by text.
    return [{"id": str(m.id), "seq": m.seq, "role": m.role, "content": m.content} for pair in kept for m in pair]


def lock_space(session, user_id, space_id) -> Space:
    """Lock order step 2: the Space row (FOR NO KEY UPDATE)."""
    space = session.scalar(
        select(Space).where(Space.id == space_id, Space.user_id == user_id).with_for_update(key_share=True)
    )
    if space is None:
        raise NotFound()
    return space


def lock_conversation(session, user_id, space_id, conversation_id) -> Conversation:
    """Lock order step 3: the conversation row (FOR UPDATE)."""
    return _owned_conversation(session, user_id, space_id, conversation_id, lock=True)


def _require_attempt(attempt) -> uuid.UUID:
    if not isinstance(attempt, uuid.UUID):
        raise InvalidAttempt()
    return attempt


def _holds_lease(conversation: Conversation, attempt: uuid.UUID) -> bool:
    """This attempt holds the conversation's live lease. Never None == None."""
    return (
        conversation.active_request_id is not None
        and conversation.active_until is not None
        and conversation.active_request_id == attempt
    )


def begin_send(session, user_id, space_id, conversation_id, request_id: uuid.UUID, message: str,
               now: datetime, lease_seconds: int, check=lambda: None) -> Replay | Prepared:
    """Transaction A: lock the conversation, replay a completed key, refuse a
    live lease or a full conversation, take the lease under a fresh server
    attempt token, read the context.

    check() runs once the conversation is locked (the send's budget): a send
    that waited past its deadline takes no lease."""
    conversation = lock_conversation(session, user_id, space_id, conversation_id)
    check()

    stored = session.scalar(select(Message).where(
        Message.conversation_id == conversation.id, Message.user_id == user_id,
        Message.client_request_id == request_id,
    ))
    if stored is not None:
        if stored.content != message:
            raise IdempotencyConflict()
        reply = session.scalar(select(Message).where(
            Message.conversation_id == conversation.id, Message.user_id == user_id,
            Message.seq == stored.seq + 1,
        ))
        return Replay(message_json(stored), message_json(reply), conversation_json(conversation))

    if conversation.active_request_id is not None and as_utc(conversation.active_until) > now:
        raise Busy()
    if conversation.message_count + 2 > MAX_MESSAGES_PER_CONVERSATION:
        raise Full()

    space = _owned_space(session, user_id, space_id)
    attempt = uuid.uuid4()
    conversation.active_request_id = attempt
    conversation.active_until = now + timedelta(seconds=lease_seconds)
    history = history_window(session, user_id, conversation.id)
    session.flush()
    return Prepared(instructions=space.instructions, history=history, attempt=attempt)


def lock_lease(session, user_id, space_id, conversation_id, attempt) -> Conversation:
    """Lock the conversation and require that `attempt` holds its live lease
    (Busy otherwise; InvalidAttempt for a missing or malformed token)."""
    _require_attempt(attempt)
    conversation = lock_conversation(session, user_id, space_id, conversation_id)
    if not _holds_lease(conversation, attempt):
        raise Busy()
    return conversation


def insert_exchange(session, conversation: Conversation, user_id, request_id: uuid.UUID, message: str,
                    reply: str, now: datetime) -> tuple[Message, Message]:
    """Insert the user message and the reply, and flush them."""
    seq = conversation.message_count + 1
    user_row = Message(conversation_id=conversation.id, user_id=user_id, seq=seq, role="user",
                       content=message, client_request_id=request_id, created_at=now)
    reply_row = Message(conversation_id=conversation.id, user_id=user_id, seq=seq + 1, role="assistant",
                        content=reply, client_request_id=None, created_at=now)
    session.add_all([user_row, reply_row])
    session.flush()
    return user_row, reply_row


def complete_exchange(session, conversation: Conversation, space: Space, rows, now: datetime) -> Replay:
    """Advance the conversation, clear its lease, touch the Space; flush."""
    user_row, reply_row = rows
    conversation.message_count = reply_row.seq
    conversation.last_message_at = now
    conversation.updated_at = now
    conversation.active_request_id = None
    conversation.active_until = None
    space.updated_at = now
    session.flush()
    return Replay(message_json(user_row), message_json(reply_row), conversation_json(conversation))


def _no_op(*args):
    return None


def finish_send(session, user_id, space_id, conversation_id, attempt, request_id: uuid.UUID,
                message: str, reply: str, now: datetime, *, bound=_no_op, lock_account=_no_op,
                verify=_no_op, settle=_no_op) -> Replay:
    """Transaction B, in the global lock order, one bounded phase at a time.

    bound(phase) runs before and after every blocking phase: the caller
    recomputes what remains of the send's budget (and its database timeouts)
    and raises to stop. verify() rechecks the session and access after the
    locks are held and again before commit; settle() writes the quota charge.
    Anything raised rolls the whole transaction back: the exchange and its
    charge are durable together, at commit, or not at all."""
    _require_attempt(attempt)
    bound("account lock")
    lock_account()
    bound("space lock")
    space = lock_space(session, user_id, space_id)
    bound("conversation lock")
    conversation = lock_lease(session, user_id, space_id, conversation_id, attempt)
    if conversation.message_count + 2 > MAX_MESSAGES_PER_CONVERSATION:
        raise Full()
    bound("authorization")
    verify()
    bound("settlement")
    settle()
    bound("after settlement")
    rows = insert_exchange(session, conversation, user_id, request_id, message, reply, now)
    bound("after message flush")
    result = complete_exchange(session, conversation, space, rows, now)
    bound("after space update")
    verify()
    bound("before commit")
    return result


def release_lease(session, user_id, space_id, conversation_id, attempt) -> bool:
    """Clear the lease only if this attempt still holds it; a missing,
    malformed or stale token changes nothing. Returns whether the
    conversation still exists."""
    conversation = session.scalar(
        select(Conversation).where(*_conversation_filters(user_id, space_id, conversation_id)).with_for_update()
    )
    if conversation is None:
        return False
    if isinstance(attempt, uuid.UUID) and _holds_lease(conversation, attempt):
        conversation.active_request_id = None
        conversation.active_until = None
    return True
