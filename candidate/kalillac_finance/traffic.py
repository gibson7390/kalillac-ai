"""The single authoritative traffic-class resolution seam.

    not authenticated                                  -> ANONYMOUS (FREE scope)
    authenticated                                      -> FREE_ACCOUNT (FREE scope)
    authenticated + a PaidEntitlement for THIS request -> PAID_ACCOUNT (PAID scope)

Paid is never inferred from an account, a cookie, a user ID, a successful
sign-in or the existence of billing rows. It requires a PaidEntitlement
issued by issue_paid_entitlement() from an EffectiveEntitlement whose tier
is paid with higher-usage access, bound to the exact request it was issued
for; anything else (missing, stale, foreign, malformed or unavailable
entitlement state) resolves to FREE. No production code issues one yet:
accounts and billing are not live.
"""
from .scopes import TrafficClass


class PaidEntitlement:
    """Opaque, request-bound evidence of an authoritative active paid entitlement."""

    __slots__ = ("_request",)

    def __init__(self, request, _key=None):
        if _key is not _ISSUE:
            raise TypeError("Use issue_paid_entitlement().")
        self._request = request


_ISSUE = object()


def issue_paid_entitlement(request, effective):
    """Evidence for `request`, or None unless `effective` is an authoritative paid tier."""
    from kalillac_accounts.entitlements import EffectiveEntitlement
    from kalillac_db.models.account_entitlement import TIER_PAID
    if request is None or type(effective) is not EffectiveEntitlement:
        return None
    if effective.tier != TIER_PAID or effective.higher_usage_access is not True:
        return None
    return PaidEntitlement(request, _ISSUE)


def resolve_traffic_class(*, authenticated, request=None, paid_entitlement=None):
    if authenticated is not True:
        return TrafficClass.ANONYMOUS
    if (type(paid_entitlement) is PaidEntitlement and request is not None
            and paid_entitlement._request is request):
        return TrafficClass.PAID_ACCOUNT
    return TrafficClass.FREE_ACCOUNT
