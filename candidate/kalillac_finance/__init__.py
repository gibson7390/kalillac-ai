"""Financial Safeguards V1, batch 1: pure money, pricing and cost primitives.

Not imported by the application in this batch. Nothing here touches a
database, the network, threads or a provider. Only pricing.load_pricing
reads configuration, and only when a caller invokes it explicitly.

Modules:
    money    exact integer micro-US-dollar arithmetic and bounded parsing
    pricing  explicit, default-free price configuration
    usage    normalized integer provider usage (never zero for unknown)
    cost     actual cost, worst-case reservation and settlement
    scopes   ledger scope identifiers: global, free and paid
"""
