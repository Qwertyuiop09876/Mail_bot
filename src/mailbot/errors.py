"""Exception hierarchy.

Everything the library raises on purpose derives from :class:`MailbotError`, so callers
(a future CLI, bot or web UI) can catch one base class and show the message to a human.
"""

from __future__ import annotations


class MailbotError(Exception):
    """Base class for all expected errors."""


class ConfigError(MailbotError):
    """Missing or invalid configuration (settings, secret key, ...)."""


class NotFoundError(MailbotError):
    """A referenced object (account, list, campaign, ...) does not exist."""


class ValidationError(MailbotError):
    """User-supplied data is invalid."""


class LoginFailedError(ValidationError):
    """The mail server did not accept the credentials (or could not be reached)."""


class StateError(MailbotError):
    """The requested operation is not allowed in the object's current state."""


class DeliveryError(MailbotError):
    """A single message could not be delivered. Subclasses tell the dispatcher what to do next."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class TransientDeliveryError(DeliveryError):
    """Temporary problem (4xx, network, timeout): the delivery is retried with backoff."""


class RecipientRejected(DeliveryError):
    """The server says this address does not exist: a hard bounce. The contact gets suppressed."""


class MessageRejected(DeliveryError):
    """Permanent rejection of this message (content/policy). Not retried, contact stays active."""


class AccountError(DeliveryError):
    """Problem with the sender account itself (bad password, sender not allowed, quota).

    Retrying other recipients is pointless, so the dispatcher pauses the campaign.
    """
