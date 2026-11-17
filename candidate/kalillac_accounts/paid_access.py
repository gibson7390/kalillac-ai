"""The one seam from a verified subscription to the PAID financial scope (Milestone 3, blueprint B2).

A request is paid only when all of these hold, decided per request and never cached:

1. V1 account security is on, and the request's cookie authorizes its exact original session through
   V1-01's read (fresh READ COMMITTED reads under the account row lock: live session, epoch, active,
   verified, not quarantined; deletion-requested accounts have no live session);
2. inside that same read, the account's effective entitlement (kalillac_accounts.entitlements) is the
   paid tier with higher-usage access: Stripe-verified, unexpired, never inferred from a cookie, an id,
   a sign-in or the existence of billing rows;
3. the account the request was resolved to (the usage meter, quota or a Space binding) is exactly that
   account;
4. the evidence is issued for this request's own provider-control state and nothing else
   (kalillac_finance.traffic.issue_paid_entitlement), so it cannot leak to another request.

Anything else (V1 off, no cookie, any refusal, an unavailable read, a mismatch) is not paid, and the
request keeps its FREE classification. Failures carry the exception class only.
"""
from __future__ import annotations

from dataclasses import dataclass
import uuid


@dataclass(frozen=True, repr=False)
class VerifiedAccount:
    """A V1-authorized account and its effective entitlement, read in one authorized transaction."""

    account_id: uuid.UUID
    effective: object                  # kalillac_accounts.entitlements.EffectiveEntitlement

    def __repr__(self):
        return "VerifiedAccount()"

    @property
    def paid(self) -> bool:
        from kalillac_accounts.entitlements import EffectiveEntitlement
        from kalillac_db.models.account_entitlement import TIER_PAID
        return (type(self.effective) is EffectiveEntitlement and self.effective.tier == TIER_PAID
                and self.effective.higher_usage_access is True)


async def verified_account(request, v1_security, cookie_name):
    """The request's V1-authorized account with its effective entitlement, or None. Never raises
    (cancellation aside)."""
    from kalillac_accounts.auth import RequestRejected, session_token_hash
    from kalillac_accounts.entitlements import effective_entitlement
    from kalillac_accounts.v1_security import V1SecurityUnavailable
    from kalillac_db.repositories.entitlements import get_entitlement
    if v1_security is None:
        return None
    try:
        token_hash = session_token_hash(request.cookies, cookie_name)
        if token_hash is None:
            return None

        def work(session, account):
            return VerifiedAccount(account.id, effective_entitlement(get_entitlement(session, account.id),
                                                                     account.authorized_at))
        found = await v1_security.read_async(token_hash, work)
        return found if type(found) is VerifiedAccount and type(found.account_id) is uuid.UUID else None
    except (RequestRejected, V1SecurityUnavailable):
        return None
    except Exception as error:
        print("WARN: PAID_ACCESS_READ_FAILED", type(error).__name__)
        return None


def resolved_account(meter_account_id, verified):
    """The account a chat request runs for: the meter's or quota's, else the V1-verified one."""
    return meter_account_id if meter_account_id is not None else (verified.account_id if verified else None)


def paid_evidence(control_request, verified, account_id):
    """PaidEntitlement evidence bound to control_request, or None unless the verified account is paid
    and is exactly account_id."""
    from kalillac_finance.traffic import issue_paid_entitlement
    if (control_request is None or type(verified) is not VerifiedAccount or account_id is None
            or account_id != verified.account_id or not verified.paid):
        return None
    return issue_paid_entitlement(control_request, verified.effective)
