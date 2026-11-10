"""Quota integration primitives shared by account and anonymous chat quotas.

Dependency-light on purpose: no account router, tokens, passwords,
entitlements, settings, database repositories or argon2, so the anonymous
allowance can use them with accounts disabled.
"""
from starlette.responses import JSONResponse


class QuotaAction:
    """Adapter reusing the app's bounded account-work admission."""
    def __init__(self, action):
        self.action = action

    async def resolve_request_account(self, ignored):
        return await self.action()


class QuotaResponse(JSONResponse):
    """Identical response bytes; compensate a known failed send only."""
    def __init__(self, original, state, release, record_sent):
        self.status_code, self.media_type = original.status_code, original.media_type
        self.background, self.body = None, original.body
        self._character_meter = original.background
        self.raw_headers = list(original.raw_headers)
        self._state, self._release, self._record_sent = state, release, record_sent

    async def __call__(self, scope, receive, send):
        sent = False
        async def checked_send(message):
            nonlocal sent
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                sent = True
        try:
            await super().__call__(scope, receive, checked_send)
        except BaseException:
            if not sent:
                self._release(self._state)
            raise

        await self._record_sent(self._state)
        if self._character_meter is not None:
            await self._character_meter()
