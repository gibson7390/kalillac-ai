"""Provider-neutral email boundary. No adapter is supplied or selected by environment."""
from typing import Protocol, runtime_checkable


class EmailDeliveryError(Exception):
    """Fixed error only: never accepts a provider message/response."""

    def __init__(self):
        super().__init__("EMAIL_DELIVERY_FAILED")


@runtime_checkable
class EmailGateway(Protocol):
    def send_password_reset(self, to: str, link: str) -> None:
        """Deliver with a bounded call; no argument may be logged or persisted here."""

    def send_password_changed(self, to: str) -> None:
        """Notify after commit; failure must not undo a successful reset."""


@runtime_checkable
class EmailVerificationGateway(Protocol):
    """Separate from EmailGateway so the recovery contract is unchanged; one
    object may implement both."""

    def send_email_verification(self, to: str, link: str) -> None:
        """Deliver with a bounded call: the account row stays locked until
        delivery and commit finish. No argument may be logged or persisted."""
