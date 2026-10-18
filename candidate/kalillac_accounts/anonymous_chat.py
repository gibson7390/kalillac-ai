"""/api/chat wiring for the anonymous daily allowance.

A request with no account cookie (or with accounts disabled) is anonymous.
Its visitor is identified only by the __Host-kalillac_visitor cookie: a
random 256-bit token, minted when the cookie is missing or malformed and
re-sent on every response to refresh its 48-hour lifetime. The token lives
only in that header and in this request's memory; it never reaches a body,
log, chat history, provider call or exception.

Nothing here imports the account stack (router, passwords, tokens or the
account quota), so the allowance works with accounts disabled. Responses are
wrapped by the same kalillac_accounts.quota_runtime.QuotaResponse that
account quotas use.
"""
from kalillac_accounts.anonymous_quota import (
    VISITOR_COOKIE, VISITOR_MAX_AGE, new_visitor_token, parse_visitor_token,
)


class AnonymousVisit:
    """One anonymous request's visitor cookie and quota request."""

    def __init__(self, service, cookies):
        presented = cookies.get(VISITOR_COOKIE)
        cookie = presented if parse_visitor_token(presented) is not None else new_visitor_token()
        self._cookie = cookie
        self.request = service.new_request(parse_visitor_token(cookie))

    def __repr__(self):
        return "AnonymousVisit()"

    def attach_cookie(self, response):
        # Always the exact __Host- form: Secure, host-only (no Domain), Path=/.
        response.set_cookie(
            key=VISITOR_COOKIE,
            value=self._cookie,
            max_age=VISITOR_MAX_AGE,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
