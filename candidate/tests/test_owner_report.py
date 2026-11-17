"""O1, the owner's read-only report: counts from one snapshot, never a person's data, never a write.

SQLite with synthetic rows; tests/test_owner_report_postgres.py runs it on PostgreSQL as the runtime login
under the final grants."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import io
import json
import os
import sys
from pathlib import Path
import uuid

import pytest
from sqlalchemy import event

from account_test_db import make_session_factory, make_sqlite_engine
from kalillac_db.models import (AccountBilling, AccountEntitlement, AccountSession, AccountUsageDaily,
                                AnonymousUsageDay, Base, Conversation, Message, Space, User)
from kalillac_db.models.billing_case import BillingCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import owner_report  # noqa: E402

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
EMAIL = "private.person@example.test"


def user(session, days_old, verified=True, quarantined=False):
    account = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}-{EMAIL}", password_hash="synthetic",
                   created_at=NOW - timedelta(days=days_old),
                   email_verified_at=NOW - timedelta(days=days_old) if verified else None)
    session.add(account)
    session.flush()
    if quarantined:
        account.quarantined_at = NOW
    return account


@pytest.fixture
def factory():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session, session.begin():
        a, b, c = user(session, 2), user(session, 20), user(session, 90, verified=False, quarantined=True)
        session.add_all([
            AccountSession(user_id=a.id, token_hash="t" * 64, created_at=NOW - timedelta(hours=1),
                           expires_at=NOW + timedelta(days=1)),
            AccountSession(user_id=b.id, token_hash="u" * 64, created_at=NOW - timedelta(days=3),
                           expires_at=NOW - timedelta(days=1)),
            AccountEntitlement(user_id=a.id, tier="paid", source="stripe", expires_at=NOW + timedelta(days=20)),
            AccountEntitlement(user_id=b.id, tier="paid", source="stripe", expires_at=NOW - timedelta(days=1)),
            AccountEntitlement(user_id=c.id, tier="free", source="registration"),
            AccountBilling(user_id=a.id, stripe_customer_id="cus_SECRET", subscription_status="active",
                           cancel_at_period_end=True),
            AccountBilling(user_id=b.id, stripe_customer_id="cus_OTHER", subscription_status="canceled"),
            BillingCase(user_id=a.id, kind="refund", provider_object_id="re_SECRET", provider_status="succeeded",
                        last_event_id="evt_1"),
            AccountUsageDaily(user_id=a.id, usage_date=NOW.date(), successful_chats=4),
            AccountUsageDaily(user_id=b.id, usage_date=NOW.date(), successful_chats=1),
            AccountUsageDaily(user_id=a.id, usage_date=NOW.date() - timedelta(days=2), successful_chats=7),
            AccountUsageDaily(user_id=a.id, usage_date=NOW.date() - timedelta(days=9), successful_chats=99),
            AnonymousUsageDay(visitor_digest="v" * 64, usage_date=NOW.date()),
            AnonymousUsageDay(visitor_digest="w" * 64, usage_date=NOW.date() - timedelta(days=1)),
        ])
        space = Space(user_id=a.id, name="Private plans", instructions="")
        session.add(space)
        session.flush()
        conversation = Conversation(space_id=space.id, user_id=a.id, title="Secret title")
        session.add(conversation)
        session.flush()
        session.add(Message(conversation_id=conversation.id, user_id=a.id, seq=1, role="user",
                            content="A private message", client_request_id=uuid.uuid4()))
    yield factory
    engine.dispose()


def report(factory, *args):
    out = io.StringIO()
    code = owner_report.run(["report", *args], factory=factory, clock=lambda: NOW, out=out)
    return code, out.getvalue()


def test_the_report_counts_from_one_snapshot(factory):
    code, text = report(factory, "--json")
    assert code == 0
    sections = json.loads(text)["sections"]
    assert sections["accounts"] == {"total": 3, "verified": 2, "quarantined": 1, "deletion_requested": 0,
                                    "new_7_days": 1, "new_30_days": 2, "live_sessions": 1}
    assert sections["plan"] == {"paid_now": 1}                 # The expired paid row is free.
    assert sections["subscriptions"] == {"by_status": {"active": 1, "canceled": 1}, "ending_at_period_end": 1}
    assert sections["billing_cases"] == {"refund open": 1}
    assert sections["usage"]["today"] == {"date": "2026-11-17", "accounts": 2, "chats": 5}
    assert [d["chats"] for d in sections["usage"]["last_7_days"]] == [0, 0, 0, 0, 7, 0, 5]
    assert sections["anonymous"] == {"visitors_today": 1}
    assert sections["saved_work"] == {"spaces": 1, "conversations": 1, "messages": 1}
    # The security events table belongs to PostgreSQL's V1 schema only.
    assert sections["security_events_7_days"] == {"unavailable": "not in this database"}


def test_nothing_about_a_person_is_ever_reported(factory, tmp_path):
    page = tmp_path / "report.html"
    for args in ((), ("--json",), ("--html", str(page))):
        code, text = report(factory, *args)
        assert code == 0
        for private in ("private.person", "cus_SECRET", "re_SECRET", "Private plans", "Secret title",
                        "A private message", "synthetic"):
            assert private not in text
            if page.exists():
                assert private not in page.read_text(encoding="utf-8")
        with factory() as session:
            for account_id in session.scalars(__import__("sqlalchemy").select(User.id)):
                assert str(account_id) not in text and account_id.hex not in text


def test_the_report_never_writes(factory):
    engine = factory.kw["bind"]
    writes = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"):
            writes.append(statement)
    assert report(factory)[0] == 0
    assert writes == []


def test_the_html_page_is_self_contained_and_new(factory, tmp_path):
    page = tmp_path / "report.html"
    assert report(factory, "--html", str(page))[0] == 0
    content = page.read_text(encoding="utf-8")
    assert "<script" not in content and "http://" not in content and "https://" not in content
    assert "default-src 'none'" in content and "Accounts" in content and "prefers-color-scheme:dark" in content
    if os.name == "posix":
        assert page.stat().st_mode & 0o777 == 0o600
    before = page.read_bytes()
    assert report(factory, "--html", str(page))[0] == 2                 # Never overwritten.
    assert page.read_bytes() == before


def test_a_section_the_role_may_not_read_is_reported_unavailable_and_the_rest_still_are(factory):
    class Denied(Exception):
        orig = type("Orig", (), {"sqlstate": "42501"})()

    def deny(session):
        raise Denied()
    result = owner_report._section(factory(), deny)
    assert result == owner_report.Unavailable("not permitted for this database role")
    assert owner_report._section(factory(), lambda s: (_ for _ in ()).throw(ValueError("x"))).reason == "not readable"


def test_refusals(monkeypatch, tmp_path):
    assert owner_report.run(["report", "--bogus"], out=io.StringIO()) == 2
    import kalillac_db.engine as engine
    monkeypatch.setattr(engine, "get_session_factory", lambda: None)      # The database is not enabled.
    assert owner_report.run(["report"], out=io.StringIO()) == 2
    existing = tmp_path / "taken.html"
    existing.write_text("keep", encoding="utf-8")
    assert owner_report.run(["report", "--html", str(existing)], out=io.StringIO()) == 2
    assert existing.read_text(encoding="utf-8") == "keep"
