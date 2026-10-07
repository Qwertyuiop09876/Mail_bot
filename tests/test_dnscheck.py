from __future__ import annotations

from mailbot.dnscheck import (
    DnsLookupError,
    Finding,
    check_domain,
    evaluate_dkim,
    evaluate_dmarc,
    evaluate_spf,
)


def levels(findings: list[Finding]) -> list[tuple[str, str]]:
    return [(f.check, f.level) for f in findings]


def test_spf() -> None:
    assert levels(evaluate_spf(["v=spf1 include:_spf.yandex.net ~all"])) == [("SPF", "ok")]
    assert levels(evaluate_spf([])) == [("SPF", "error")]
    assert levels(evaluate_spf(["v=spf1 a", "v=spf1 mx -all"])) == [("SPF", "error")]
    assert levels(evaluate_spf(["v=spf1 +all"])) == [("SPF", "error")]
    assert levels(evaluate_spf(["v=spf1 a mx"])) == [("SPF", "warn")]
    assert levels(evaluate_spf(["v=spf1 redirect=_spf.yandex.net"])) == [("SPF", "ok")]


def test_spf_yandex_hint() -> None:
    bad = evaluate_spf(["v=spf1 a ~all"], provider="yandex360")
    assert ("SPF", "error") in levels(bad) and "_spf.yandex.net" in bad[-1].message
    assert levels(evaluate_spf(["v=spf1 include:_spf.yandex.net ~all"], provider="yandex360")) == [
        ("SPF", "ok")
    ]


def test_dkim() -> None:
    assert evaluate_dkim("mail", ["v=DKIM1; k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4"]).level == "ok"
    assert evaluate_dkim("mail", []).level == "warn"
    assert evaluate_dkim("mail", ["v=DKIM1; p="]).level == "error"


def test_dmarc() -> None:
    assert evaluate_dmarc(["v=DMARC1; p=reject; rua=mailto:a@b.ru"]).level == "ok"
    assert evaluate_dmarc(["v=DMARC1; p=none"]).level == "warn"
    assert evaluate_dmarc([]).level == "warn"
    assert evaluate_dmarc(["v=DMARC1; rua=mailto:a@b.ru"]).level == "error"


def test_check_domain_queries_the_right_names() -> None:
    zone = {
        "example.com": ["v=spf1 include:_spf.yandex.net ~all"],
        "mail._domainkey.example.com": ["v=DKIM1; k=rsa; p=ABC"],
        "_dmarc.example.com": ["v=DMARC1; p=quarantine"],
    }
    asked: list[str] = []

    def lookup(name: str) -> list[str]:
        asked.append(name)
        return zone.get(name, [])

    findings = check_domain("example.com", provider="yandex", lookup=lookup)
    assert all(f.level == "ok" for f in findings) and len(findings) == 3
    assert asked == ["example.com", "mail._domainkey.example.com", "_dmarc.example.com"]


def test_dns_outage_is_reported_not_raised() -> None:
    def lookup(_name: str) -> list[str]:
        raise DnsLookupError("timeout")

    (finding,) = check_domain("example.com", lookup=lookup)
    assert finding.level == "error" and "timeout" in finding.message
