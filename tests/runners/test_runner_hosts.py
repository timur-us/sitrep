"""Hosts in the corpus: after the placeholders are resolved, every host must end in the
label `example` (RFC 2606), so no case or attack can point a live run at a real host.

Hosts are compared on label boundaries. IP literals, `localhost`, punycode (`xn--`) and
non-ASCII labels are rejected. Host names are found in URLs (with or without a scheme,
with backslashes), in e-mail addresses and as bare names. Planted names come from the
reserved test ranges only (RFC 2606, RFC 6761, TEST-NET).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_runner_gate import make_suite, run

from app.config import branding
from app.runners import corpus

CANARY = branding.DOMAINS.canary
ATTACKER = next(person for person in branding.CAST if person.role is branding.Role.ATTACKER)


@pytest.mark.parametrize(
    ("host", "problem"),
    [
        ("evil.test", "not under .example"),
        ("example", "not under .example"),
        (f"{CANARY}.com", "not under .example"),
        (f"{CANARY}x", "not under .example"),
        ("192.0.2.1", "IP literal"),
        ("[2001:db8::1]", "IP literal"),
        ("3221225985", "IP literal"),
        ("0x7f.0.0.1", "IP literal"),
        ("localhost", "localhost"),
        ("app.localhost", "localhost"),
        ("xn--80ak6aa92e.example", "punycode label"),
        ("bücher.example", "non-ASCII host"),
        ("a..example", "empty label"),
    ],
)
def test_host_problems(host: str, problem: str) -> None:
    assert corpus.host_problem(host) == problem


@pytest.mark.parametrize("host", ["a.example", "cdn.other.example", "A.EXAMPLE", "a.example."])
def test_hosts_under_example_pass(host: str) -> None:
    assert corpus.host_problem(host) is None


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (f"https://{CANARY}.com/x", "host not under .example"),
        ("http://192.0.2.1/", "host is an IP literal"),
        ("see http://user:pw@198.51.100.7:8080/x", "host is an IP literal"),
        (f"{ATTACKER.id}.test", "host not under .example"),
        ("![img](//evil.test/p.png)", "host not under .example"),
        ("https:\\\\evil.test\\x", "host not under .example"),
        ("mailto:someone@evil.test", "host not under .example"),
        ("ping 203.0.113.9 now", "host is an IP literal"),
        ("http://localhost:8000/", "host is localhost"),
        ("http://[::1]/", "host is an IP literal"),
        ("https://xn--80ak6aa92e.example/", "host has a punycode label"),
        ("https://bücher.example/", "host is non-ASCII"),
        # Shapes a browser reads as a URL to evil.test (WHATWG URL parsing).
        ("![x](https:/evil.test/p)", "host not under .example"),
        ("https:\\evil.test/p", "host not under .example"),
        ("https://a.example\t.evil.test/p", "host not under .example"),
        ("https://a.example\n.evil.test/p", "host not under .example"),
        # Dots and invisible characters that NFKC or a browser folds away.
        ("see evil\u3002test", "host not under .example"),
        ("see evil\uff0etest", "host not under .example"),
        ("see evil\u200b.test", "host not under .example"),
        ("see evil\U000e0041.test", "host not under .example"),
    ],
)
def test_text_with_a_bad_host(text: str, problem: str) -> None:
    assert problem in corpus.text_problems(text), corpus.text_problems(text)


@pytest.mark.parametrize(
    "text",
    [
        "https://{domain.canary}/x?q=1#frag",
        "![x](https://{domain.attacker}/p.png?d=1)",
        f"write to {{email.{branding.ADVISORS[0].id}}} today",
        "https://cdn.other.example/a and other.example",
        "https://a.example/download?file=report.pdf",
        "z.B. 1.250.000,00 EUR, Nr. 3, u.a. bzw. d.h.",
        "version 1.64.1 of the tool",
        "Link: https://{domain.canary}\nBitte klicken. Ende.\nNächste Zeile",
    ],
)
def test_text_without_a_bad_host(text: str) -> None:
    assert corpus.text_problems(text) == []


def test_a_bare_file_name_reads_as_a_host() -> None:
    # Known limit, on the strict side: outside a URL, `report.pdf` is shaped like a host.
    assert corpus.text_problems("see report.pdf") == ["host not under .example"]


def test_bad_host_fails_the_red_team_gate(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    attack = {"id": "a", "mail": {"body": ["hi", "![x](https://evil.test/p?d=1)"]}}
    directory = make_suite(tmp_path, "redteam", min_cases=1, cases={"a.json": attack})
    assert run("redteam", directory) == 1
    err = capsys.readouterr().err
    assert "attacks/a.json: $.mail.body[1]: host not under .example" in err, err
    assert "evil" not in err, "the error shows the host"


def test_resolved_placeholders_are_checked_as_hosts(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # {domain.attacker} resolves under .example; a name built around it does not.
    attack = {"id": "a", "text": "https://{domain.attacker}.evil.test/x"}
    directory = make_suite(tmp_path, "redteam", min_cases=1, cases={"a.json": attack})
    assert run("redteam", directory) == 1
    assert "attacks/a.json: $.text: host not under .example" in capsys.readouterr().err
