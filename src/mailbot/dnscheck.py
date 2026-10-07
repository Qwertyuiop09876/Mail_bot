"""Sender-domain DNS health: SPF, DKIM and DMARC.

These three records decide whether big mailbox providers accept your mail or send it to spam.
The evaluation functions are pure (text in, findings out) so they are testable offline.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import dns.exception
import dns.resolver

Level = Literal["ok", "warn", "error"]
TxtLookup = Callable[[str], list[str]]


@dataclass(frozen=True)
class Finding:
    check: str
    level: Level
    message: str


class DnsLookupError(Exception):
    """DNS could not be queried (timeout, no network) — different from "record not found"."""


def dns_txt_lookup(name: str) -> list[str]:
    """TXT records for ``name`` (multi-string records joined); ``[]`` if there are none."""
    try:
        answer = dns.resolver.resolve(name, "TXT", lifetime=5.0)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except dns.exception.DNSException as exc:
        raise DnsLookupError(f"{name}: {exc}") from exc
    return ["".join(s.decode("utf-8", "replace") for s in rdata.strings) for rdata in answer]


def evaluate_spf(records: Sequence[str], *, provider: str | None = None) -> list[Finding]:
    spf = [r for r in records if r.lower().startswith("v=spf1")]
    if not spf:
        return [
            Finding(
                "SPF",
                "error",
                "No SPF record: receivers can't verify your server is allowed to send.",
            )
        ]
    if len(spf) > 1:
        return [
            Finding("SPF", "error", "Several SPF records: that is invalid, merge them into one.")
        ]
    record = spf[0].lower()
    findings: list[Finding] = []
    terms = record.split()
    if "+all" in terms or "?all" in terms:
        findings.append(
            Finding(
                "SPF", "error", f"Permissive SPF ending ({terms[-1]}): anyone can spoof the domain."
            )
        )
    elif "-all" in terms or "~all" in terms or any(t.startswith("redirect=") for t in terms):
        findings.append(Finding("SPF", "ok", "SPF record present."))
    else:
        findings.append(
            Finding("SPF", "warn", "SPF has no 'all' mechanism; end it with ~all or -all.")
        )
    if provider and provider.startswith("yandex") and "_spf.yandex.net" not in record:
        findings.append(
            Finding(
                "SPF",
                "error",
                "Yandex mailbox on this domain, but SPF doesn't include _spf.yandex.net.",
            )
        )
    return findings


def evaluate_dkim(selector: str, records: Sequence[str]) -> Finding:
    check = f"DKIM[{selector}]"
    dkim = [r for r in records if "v=dkim1" in r.lower().replace(" ", "")]
    if not dkim:
        return Finding(
            check,
            "warn",
            f"No DKIM key at selector '{selector}'. Check the selector name with your provider.",
        )
    compact = dkim[0].replace(" ", "").lower()
    if "p=;" in compact or compact.endswith("p="):
        return Finding(check, "error", "DKIM key is revoked (empty p=).")
    return Finding(check, "ok", "DKIM key published.")


def evaluate_dmarc(records: Sequence[str]) -> Finding:
    dmarc = [r for r in records if r.lower().replace(" ", "").startswith("v=dmarc1")]
    if not dmarc:
        return Finding(
            "DMARC",
            "warn",
            "No DMARC record. Gmail and Yahoo require it from bulk senders; start with p=none.",
        )
    policy = next(
        (
            t.split("=", 1)[1]
            for t in dmarc[0].lower().replace(" ", "").split(";")
            if t.startswith("p=")
        ),
        "",
    )
    if policy in ("quarantine", "reject"):
        return Finding("DMARC", "ok", f"DMARC policy p={policy}.")
    if policy == "none":
        return Finding(
            "DMARC",
            "warn",
            "DMARC p=none only monitors; tighten to quarantine once reports look clean.",
        )
    return Finding("DMARC", "error", "DMARC record has no valid p= policy.")


def check_domain(
    domain: str,
    *,
    dkim_selectors: Sequence[str] = ("mail",),
    provider: str | None = None,
    lookup: TxtLookup = dns_txt_lookup,
) -> list[Finding]:
    """Run all DNS checks for ``domain``. ``provider`` (``yandex``/``yandex360``) enables extras."""
    findings: list[Finding] = []
    try:
        findings += evaluate_spf(lookup(domain), provider=provider)
        for selector in dkim_selectors:
            findings.append(evaluate_dkim(selector, lookup(f"{selector}._domainkey.{domain}")))
        findings.append(evaluate_dmarc(lookup(f"_dmarc.{domain}")))
    except DnsLookupError as exc:
        findings.append(Finding("DNS", "error", f"Lookup failed: {exc}"))
    return findings
