"""Who is asking, for /api/files: the signed-in account and whether its entitlement is paid.

The same two authentication paths as /api/account: the V1 security authority when it is configured
(it admits only active, verified accounts), otherwise the session lookup, which here also requires a
verified email address whenever email verification is enabled. Nothing is cached; every request is
checked against the database. Failures of the account store become "not signed in", never a crash and
never access.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .router import FileAccount


def build_authenticator(*, settings, v1_security=None, session_factory=None, clock=None):
    from starlette.concurrency import run_in_threadpool
    from kalillac_accounts.entitlements import effective_entitlement
    from kalillac_accounts.tokens import hash_session_token
    from kalillac_db.models.account_entitlement import TIER_PAID
    from kalillac_db.repositories.accounts import get_signed_in_user
    from kalillac_db.repositories.entitlements import get_entitlement

    clock = clock or (lambda: datetime.now(timezone.utc))

    async def authenticate(request):
        token = request.cookies.get(settings.cookie_name)
        if not token:
            return None
        if v1_security is not None:
            from kalillac_accounts.auth import RequestRejected, session_token_hash
            from kalillac_accounts.v1_security import V1SecurityUnavailable
            token_hash = session_token_hash(request.cookies, settings.cookie_name)
            if token_hash is None:
                return None

            def payload(session, account):
                tier = effective_entitlement(get_entitlement(session, account.id), account.authorized_at).tier
                return FileAccount(str(account.id), tier == TIER_PAID)
            try:
                return await v1_security.read_async(token_hash, payload)
            except (RequestRejected, V1SecurityUnavailable):
                return None

        token_hash = hash_session_token(token)
        now = clock()

        def work():
            factory = session_factory()
            if factory is None:
                return None
            with factory() as session, session.begin():
                user = get_signed_in_user(session, token_hash, now)
                if user is None or (settings.email_verification_enabled and user.email_verified_at is None):
                    return None
                tier = effective_entitlement(get_entitlement(session, user.id), now).tier
                return FileAccount(str(user.id), tier == TIER_PAID)
        return await run_in_threadpool(work)

    return authenticate
