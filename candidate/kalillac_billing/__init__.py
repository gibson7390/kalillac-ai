"""Kalillac Stripe billing (subscription checkout, portal, webhooks).

Imported only when KALILLAC_BILLING_ENABLED is set. Only stripe_gateway.py
imports the stripe package. Billing never touches /api/chat, Private
Session, or usage metering; verified webhook reconciliation is the only
billing path that changes an account's entitlement.
"""
