"""Address validation/normalisation shared by contacts, accounts and the CLI."""

from __future__ import annotations

from email_validator import EmailNotValidError, validate_email

from .errors import ValidationError


def normalize_email(raw: str) -> str:
    """Return the canonical form of an address (lower-case, IDN domain as punycode).

    Syntax only — no DNS lookups, so importing a large file stays fast and offline-safe.
    Addresses with non-ASCII local parts (SMTPUTF8) are rejected: most providers can't relay them.
    """
    candidate = (raw or "").strip().lower()
    try:
        result = validate_email(candidate, check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError as exc:
        raise ValidationError(f"Invalid email address {raw!r}: {exc}") from exc
    return result.normalized.lower()
