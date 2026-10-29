"""Provider-neutral transactional account messages: plain text and escaped HTML.

Rendered in memory for one delivery and never logged or persisted. The HTML
is self-contained: no images, fonts, scripts, stylesheets or tracking, and
the only link is the approved credential URL, used exactly as given.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from kalillac_db.repositories.email_verification import TOKEN_LIFETIME as VERIFICATION_LIFETIME

from .recovery_tokens import TOKEN_LIFETIME_SECONDS as RESET_LIFETIME_SECONDS

PRODUCT = "Kalillac AI"


@dataclass(frozen=True)
class EmailMessage:
    subject: str
    text: str = field(repr=False)
    html: str = field(repr=False)


def _duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" + ("" if hours == 1 else "s")
    minutes = seconds // 60
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


RESET_LIFETIME = _duration(RESET_LIFETIME_SECONDS)
VERIFY_LIFETIME = _duration(int(VERIFICATION_LIFETIME.total_seconds()))


def _html(subject: str, paragraphs: list[str]) -> str:
    """paragraphs are already-escaped HTML fragments."""
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{escape(subject)}</title></head><body>{body}</body></html>"
    )


def _link(url: str) -> str:
    safe = escape(url, quote=True)
    return f'<a href="{safe}">{safe}</a>'


def password_reset(link: str) -> EmailMessage:
    subject = f"Reset your {PRODUCT} password"
    text = (
        f"Someone asked to reset the password for your {PRODUCT} account.\n\n"
        "To choose a new password, open this link:\n"
        f"{link}\n\n"
        f"The link expires in {RESET_LIFETIME} and works once.\n\n"
        "If you didn't ask for this, you can ignore this email. Your password won't change.\n"
    )
    html = _html(subject, [
        f"Someone asked to reset the password for your {PRODUCT} account.",
        "To choose a new password, open this link:",
        _link(link),
        f"The link expires in {RESET_LIFETIME} and works once.",
        "If you didn&#x27;t ask for this, you can ignore this email. Your password won&#x27;t change.",
    ])
    return EmailMessage(subject, text, html)


def password_changed() -> EmailMessage:
    subject = f"Your {PRODUCT} password was changed"
    text = (
        f"The password for your {PRODUCT} account was just changed, and every device "
        "was signed out.\n\n"
        "If you made this change, you don't need to do anything.\n\n"
        f"If you didn't, start account recovery for this email address on {PRODUCT} "
        "right away to choose a new password.\n"
    )
    html = _html(subject, [
        f"The password for your {PRODUCT} account was just changed, and every device was signed out.",
        "If you made this change, you don&#x27;t need to do anything.",
        f"If you didn&#x27;t, start account recovery for this email address on {PRODUCT} "
        "right away to choose a new password.",
    ])
    return EmailMessage(subject, text, html)


def email_verification(link: str) -> EmailMessage:
    subject = f"Verify your email for {PRODUCT}"
    text = (
        f"Confirm that this email address belongs to your {PRODUCT} account.\n\n"
        "To verify it, open this link:\n"
        f"{link}\n\n"
        f"The link expires in {VERIFY_LIFETIME} and works once.\n\n"
        f"If you didn't ask to verify an email address for {PRODUCT}, you can ignore this email.\n"
    )
    html = _html(subject, [
        f"Confirm that this email address belongs to your {PRODUCT} account.",
        "To verify it, open this link:",
        _link(link),
        f"The link expires in {VERIFY_LIFETIME} and works once.",
        f"If you didn&#x27;t ask to verify an email address for {PRODUCT}, you can ignore this email.",
    ])
    return EmailMessage(subject, text, html)
