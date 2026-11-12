"""Complete Financial Safeguards configuration: prices plus ceilings.

load_financial_config() returns None, reading only the enable flag, when
KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED is off. When it is on, every price
(pricing.REQUIRED) and every ceiling (limits.REQUIRED) must be explicit and
consistent, or loading refuses with PricingConfigError before anything can
dispatch.
"""
from dataclasses import dataclass
import os

from .limits import FinancialLimits, load_limits
from .pricing import PriceTable, PricingConfigError, load_pricing


@dataclass(frozen=True)
class FinancialConfig:
    prices: PriceTable
    limits: FinancialLimits


def load_financial_config(environ=None):
    environ = os.environ if environ is None else environ
    prices = load_pricing(environ)
    if prices is None:
        return None
    return FinancialConfig(prices, load_limits(environ, PricingConfigError))
