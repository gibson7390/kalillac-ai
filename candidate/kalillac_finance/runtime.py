"""Pure glue between the provider-control seams and the ledger.

charge_for() computes one attempt's worst-case reservation from the exact
arguments of the protected seam; settlement_for() computes its settlement
from the attempt's outcome. No environment, database or network access.
"""
from dataclasses import dataclass

from .cost import (OpenAIReservation, ReservationError, reserve_brave, reserve_openai, reserve_tavily, settle,
                   settle_brave, settle_openai, settle_tavily)
from .money import MoneyError
from .scopes import FinancialScope, TrafficClass, charged_scopes

UNKNOWN = "unknown"


@dataclass(frozen=True)
class Charge:
    provider: str
    traffic_class: TrafficClass
    scope: FinancialScope
    reserved: int
    openai: OpenAIReservation | None = None


def charge_for(prices, provider, args, traffic_class):
    """The Charge for one attempt on the seam protect() wraps.

    openai: args = (payload,); brave: args = ("brave", arguments);
    tavily: args = (budget, operation, arguments).
    Raises ReservationError when the attempt cannot be bounded.
    """
    scope = charged_scopes(traffic_class)[1]
    if provider == "openai" and len(args) >= 1:
        reservation = reserve_openai(prices.openai, args[0])
        return Charge(provider, traffic_class, scope, reservation.microusd, reservation)
    if provider == "brave":
        return Charge(provider, traffic_class, scope, reserve_brave(prices.brave))
    if provider == "tavily" and len(args) == 3 and type(args[2]) is dict:
        operation = args[1]
        depth = args[2].get("search_depth" if operation == "search" else "extract_depth")
        return Charge(provider, traffic_class, scope, reserve_tavily(prices.tavily, operation, depth))
    raise ReservationError("Unpriced provider operation.")


def settlement_for(prices, charge, value, returned):
    """Settlement of a DISPATCHED attempt. A raised attempt, or a result that
    cannot be priced, keeps the full reservation charged."""
    if not returned:
        return settle(charge.reserved, None, UNKNOWN)
    try:
        if charge.provider == "openai":
            return settle_openai(prices.openai, charge.openai, value)
        if charge.provider == "brave":
            return settle_brave(prices.brave, charge.reserved)
        if charge.provider == "tavily":
            return settle_tavily(prices.tavily, charge.reserved, value)
    except MoneyError:
        pass
    return settle(charge.reserved, None, "invalid")
