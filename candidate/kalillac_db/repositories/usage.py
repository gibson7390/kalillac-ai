"""Aggregate daily usage counters.

Increments are a single INSERT ... ON CONFLICT DO UPDATE statement that adds
to the stored values inside the database. PostgreSQL resolves concurrent
upserts on the (user_id, usage_date) primary key with row locking, so two
simultaneous increments both land; there is no read-modify-write window in
application code. SQLite supports the same statement, so tests exercise the
same path.

Nothing here accepts text: only an account id, a date, and counts.
"""

from __future__ import annotations

from datetime import date
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from ..models import AccountUsageDaily


_UPSERT_INSERTS = {
    "postgresql": postgresql.insert,
    "sqlite": sqlite.insert,
}


def build_usage_increment(
    dialect_name: str,
    user_id: uuid.UUID,
    usage_date: date,
    *,
    request_chars: int,
    response_chars: int,
):
    """The atomic upsert statement for one successful chat."""

    if request_chars < 0 or response_chars < 0:
        raise ValueError("Usage counts cannot be negative.")

    try:
        insert = _UPSERT_INSERTS[dialect_name]
    except KeyError as exc:
        raise NotImplementedError(
            f"No atomic usage upsert for dialect {dialect_name!r}."
        ) from exc

    table = AccountUsageDaily.__table__

    statement = insert(table).values(
        user_id=user_id,
        usage_date=usage_date,
        successful_chats=1,
        request_chars=request_chars,
        response_chars=response_chars,
    )

    excluded = statement.excluded

    return statement.on_conflict_do_update(
        index_elements=[table.c.user_id, table.c.usage_date],
        set_={
            "successful_chats": (
                table.c.successful_chats + excluded.successful_chats
            ),
            "request_chars": table.c.request_chars + excluded.request_chars,
            "response_chars": table.c.response_chars + excluded.response_chars,
            "updated_at": func.now(),
        },
    )


def increment_daily_usage(
    session: Session,
    user_id: uuid.UUID,
    usage_date: date,
    *,
    request_chars: int,
    response_chars: int,
) -> None:
    session.execute(
        build_usage_increment(
            session.get_bind().dialect.name,
            user_id,
            usage_date,
            request_chars=request_chars,
            response_chars=response_chars,
        )
    )


def get_daily_usage(
    session: Session,
    user_id: uuid.UUID,
    usage_date: date,
) -> AccountUsageDaily | None:
    return session.scalar(
        select(AccountUsageDaily).where(
            AccountUsageDaily.user_id == user_id,
            AccountUsageDaily.usage_date == usage_date,
        )
    )
