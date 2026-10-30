"""Shared runner for the eval and red-team gates, offline.

A suite directory holds `gate.toml` (`min_cases`), the case directory (`cases/` or
`attacks/`, one `<name>.json` per case) and `recorded/<version>/<case id>.json`. M0
checks structure and scores nothing:

- an empty corpus passes when `min_cases` is 0; any case at all fails closed, because
  nothing scores it before M1;
- a stray file, a symlink, invalid JSON, a duplicate key or id, an orphan recording,
  or a recording that carries a credential header (as a key or a value) fails;
- case text is read as NFKC, with ideographic dots as "." and without invisible format
  characters. It names the cast, the bank, the product and the domains only through
  placeholders (`{advisor.<id>}`, `{domain.attacker}`, `{mandate.demo}` ...), resolved
  from branding. After resolution every host must end in the label `example`;
- `--live` needs `--yes`, and live runs are not available in M0.

Files are read with `json` and `tomllib` only. Errors name the file (escaped unless
it is a plain name), the JSON path and the rule, never the text: corpus text is untrusted (CLAUDE.md rule 9) and recordings
may carry what they must not. No network is used and no key is read.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import tomllib
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.config import branding

ROOT = Path(__file__).resolve().parents[2]

CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
VERSION = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
IGNORED = frozenset({".gitkeep", ".DS_Store"})
GATE_KEYS = frozenset({"min_cases"})
# Never in a recording, request or response, at any depth.
# As a key, or as a value (HAR-style {"name": ..., "value": ...} and [name, value] pairs).
CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "ocp-apim-subscription-key",
        "cookie",
        "set-cookie",
    }
)
# A file name is printed as it is only if it is plain; otherwise escaped, so that a
# newline in a name cannot start a GitHub workflow command ("::error ...") in the log.
PLAIN_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,128}")
# A JSON key is printed in a path only if it looks like a field name.
FIELD = re.compile(r"[a-z_][a-z0-9_]{0,31}")


@dataclass(frozen=True)
class Suite:
    name: str  # also the directory under the repository root
    case_dir: str


def placeholders() -> dict[str, str]:
    """`{namespace.key}` -> value, from branding."""
    table = {
        f"domain.{field}": getattr(branding.DOMAINS, field)
        for field in ("staff", "attacker", "canary")
    }
    for person in branding.CAST:
        table[f"cast.{person.id}"] = person.name
        table[f"email.{person.id}"] = person.email
    for person in branding.ADVISORS:
        table[f"advisor.{person.id}"] = person.name
    table["mandate.demo"] = branding.DEMO_MANDATE.family
    table["product.name"] = branding.PRODUCT.name
    table["bank.legal_name"] = branding.BANK.legal_name
    table["bank.display_name"] = branding.BANK.display_name
    return table


PLACEHOLDERS = placeholders()
NAMESPACES = frozenset(key.partition(".")[0] for key in PLACEHOLDERS)
PLACEHOLDER = re.compile(r"\{([a-z_]+)\.([a-z0-9_]+)\}")


def _word(value: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(value)}(?!\w)", re.IGNORECASE)


def _literals() -> list[tuple[str, re.Pattern[str]]]:
    rules = []
    for person in branding.CAST:
        rules += [("literal cast name", _word(value)) for value in {person.name, person.id}]
    mandate = branding.DEMO_MANDATE
    rules += [("literal demo mandate", _word(value)) for value in {mandate.family, mandate.id}]
    rules += [("literal bank name", _word(stem)) for stem in branding.BANK.name_stems]
    rules += [
        ("literal product name", _word(value)) for value in {branding.PRODUCT.name, branding.SLUG}
    ]
    for domain in (branding.DOMAINS.staff, branding.DOMAINS.attacker, branding.DOMAINS.canary):
        pattern = re.compile(rf"(?<![a-z0-9-]){re.escape(domain)}(?![a-z0-9-])", re.IGNORECASE)
        rules.append(("literal domain", pattern))
    return rules


LITERALS = _literals()

# Hosts: in URLs, after an `@`, and bare names whose last label has two letters or more
# ("z.B." is not a host). A URL is what a browser reads as one: a scheme and one or more
# slashes or backslashes, or `//` alone. Browsers drop tab, CR and LF inside a URL, so
# the host goes on across them when a dot follows (`a.example<TAB>.evil.test`).
_AUTHORITY = r"(?:[^\s/\\?#<>\"'()]|[\t\r\n]+(?=\.))*"
_URL_START = r"(?:[a-z][a-z0-9+.-]*:[/\\]+|[/\\]{2})"
URL = re.compile(rf"{_URL_START}({_AUTHORITY})", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@([\w.-]+)")
# A whole URL, removed before bare names are looked for: `?file=report.pdf` is a path.
URL_SPAN = re.compile(rf"{_URL_START}(?:\S|[\t\r\n]+(?=\.))*", re.IGNORECASE)
# Dots that are not ASCII after NFKC: ideographic and halfwidth ideographic full stops.
DOTS = str.maketrans({"\u3002": ".", "\uff61": "."})
BARE_HOST = re.compile(r"(?<![\w.@/\\-])((?:[\w-]+\.)+[^\W\d_]{2}[\w-]*)\.?(?![\w-])")
BARE_IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
HOST_MESSAGES = {
    "not under .example": "host not under .example",
    "IP literal": "host is an IP literal",
    "localhost": "host is localhost",
    "punycode label": "host has a punycode label",
    "non-ASCII host": "host is non-ASCII",
    "empty label": "host has an empty label",
}


def host_problem(host: str) -> str | None:
    """Why host is not allowed in the corpus, or None. Labels, not substrings."""
    name = host.strip().lower()
    if name.endswith(".") and not name.endswith(".."):
        name = name[:-1]  # the root label
    if name.startswith("["):
        return "IP literal"
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return "IP literal"
    if not name.isascii():
        return "non-ASCII host"
    labels = name.split(".")
    if any(not label for label in labels):
        return "empty label"
    if any(label.startswith("xn--") for label in labels):
        return "punycode label"
    if "localhost" in labels:
        return "localhost"
    if labels[-1].isdigit() or labels[-1].startswith("0x"):
        return "IP literal"
    if len(labels) < 2 or labels[-1] != "example":
        return "not under .example"
    return None


def normalize(text: str) -> str:
    """What a reader or a browser sees: NFKC (fullwidth forms, compatibility dots),
    ideographic dots as ".", and no invisible format characters (zero-width, tags)."""
    folded = unicodedata.normalize("NFKC", text).translate(DOTS)
    return "".join(char for char in folded if unicodedata.category(char) != "Cf")


def _authority_host(authority: str) -> str:
    host = re.sub(r"[\t\r\n]", "", authority).rpartition("@")[2]
    if host.startswith("["):
        return host.partition("]")[0] + "]"
    return host.partition(":")[0]


def hosts(text: str) -> list[str]:
    found = [_authority_host(match.group(1)) for match in URL.finditer(text)]
    rest = URL_SPAN.sub(" ", text)
    found += [match.group(1) for match in EMAIL.finditer(rest)]
    rest = EMAIL.sub(" ", rest)
    found += [match.group(0) for match in BARE_IPV4.finditer(rest)]
    found += [match.group(1) for match in BARE_HOST.finditer(rest)]
    return found


def text_problems(text: str) -> list[str]:
    """The rules one string breaks: placeholders, literals, then hosts after resolution."""
    text = normalize(text)
    found: list[str] = []
    for namespace, key in PLACEHOLDER.findall(text):
        if namespace in NAMESPACES and f"{namespace}.{key}" not in PLACEHOLDERS:
            found.append("unknown placeholder")

    def known(match: re.Match[str]) -> str | None:
        return PLACEHOLDERS.get(f"{match.group(1)}.{match.group(2)}")

    stripped = PLACEHOLDER.sub(lambda m: " " if known(m) is not None else m.group(0), text)
    found += [rule for rule, pattern in LITERALS if pattern.search(stripped)]
    resolved = PLACEHOLDER.sub(lambda m: known(m) or m.group(0), text)
    for host in hosts(resolved):
        if (problem := host_problem(host)) is not None:
            found.append(HOST_MESSAGES[problem])
    return list(dict.fromkeys(found))


def _strings(node: object, path: str) -> Iterator[tuple[str, str]]:
    """(JSON path, string) for every key and string value. A key that breaks a rule is
    shown as <key>, so a path never carries corpus text."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(node, dict):
        for key, value in node.items():
            clean = FIELD.fullmatch(key) is not None and not text_problems(key)
            yield f"{path}.<key>", key
            yield from _strings(value, f"{path}.{key if clean else '<key>'}")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _load_json(path: Path, rel: str, problems: list[str]) -> object | None:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        problems.append(f"{rel}: not UTF-8")
        return None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ValueError as error:
        reason = "duplicate key" if str(error) == "duplicate key" else "not valid JSON"
        problems.append(f"{rel}: {reason}")
        return None


def _shown(name: str) -> str:
    return name if PLAIN_NAME.fullmatch(name) else ascii(name)


def _entries(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.name not in IGNORED)


def _load_gate(directory: Path, problems: list[str]) -> int | None:
    path = directory / "gate.toml"
    if not path.is_file() or path.is_symlink():
        problems.append("gate.toml is missing")
        return None
    try:
        gate = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        problems.append("gate.toml is not valid TOML")
        return None
    unknown = set(gate) - GATE_KEYS
    if unknown:
        problems.append(f"gate.toml: unknown keys {sorted(unknown)}")
    min_cases = gate.get("min_cases")
    if type(min_cases) is not int or min_cases < 0:
        problems.append("gate.toml: min_cases must be an integer >= 0")
        return None
    return min_cases


def _load_cases(suite: Suite, directory: Path, problems: list[str]) -> set[str]:
    base = directory / suite.case_dir
    if not base.is_dir() or base.is_symlink():
        problems.append(f"{suite.case_dir}/ is missing")
        return set()
    ids: set[str] = set()
    for path in _entries(base):
        rel = f"{suite.case_dir}/{_shown(path.name)}"
        if path.is_symlink():
            problems.append(f"{rel}: symlink")
            continue
        if not path.is_file() or path.suffix != ".json":
            problems.append(f"{rel}: stray entry (cases are <name>.json files)")
            continue
        case = _load_json(path, rel, problems)
        if case is None:
            continue
        if not isinstance(case, dict):
            problems.append(f"{rel}: not a JSON object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
            problems.append(f"{rel}: id must be a string matching {CASE_ID.pattern}")
        elif case_id in ids:
            problems.append(f"{rel}: duplicate id")
        else:
            ids.add(case_id)
        for where, text in _strings(case, "$"):
            problems += [f"{rel}: {where}: {problem}" for problem in text_problems(text)]
    return ids


def _credential_header(node: object) -> bool:
    if isinstance(node, str):
        return node.strip().lower() in CREDENTIAL_HEADERS
    if isinstance(node, dict):
        return any(
            key.lower() in CREDENTIAL_HEADERS or _credential_header(value)
            for key, value in node.items()
        )
    if isinstance(node, list):
        return any(_credential_header(item) for item in node)
    return False


def _check_recordings(directory: Path, case_ids: set[str], problems: list[str]) -> None:
    base = directory / "recorded"
    if not base.is_dir() or base.is_symlink():
        problems.append("recorded/ is missing")
        return
    for version in _entries(base):
        rel_version = f"recorded/{_shown(version.name)}"
        if version.is_symlink() or not version.is_dir() or not VERSION.fullmatch(version.name):
            problems.append(f"{rel_version}: stray entry (recorded/ holds <version>/ directories)")
            continue
        for path in _entries(version):
            rel = f"{rel_version}/{_shown(path.name)}"
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                problems.append(f"{rel}: stray entry (recordings are <case id>.json files)")
                continue
            if path.stem not in case_ids:
                problems.append(f"{rel}: no case with this id")
            recording = _load_json(path, rel, problems)
            if recording is not None and _credential_header(recording):
                problems.append(f"{rel}: carries a credential header")


def check(suite: Suite, directory: Path) -> int:
    problems: list[str] = []
    min_cases = _load_gate(directory, problems)
    case_ids = _load_cases(suite, directory, problems)
    _check_recordings(directory, case_ids, problems)
    if min_cases is not None and len(case_ids) < min_cases:
        problems.append(f"{len(case_ids)} cases, min_cases={min_cases}: below the gate")
    if problems:
        for problem in problems:
            print(f"{suite.name}: {problem}", file=sys.stderr)
        return 1
    if not case_ids:
        print(f"{suite.name}: no cases (min_cases={min_cases})")
        return 0
    print(
        f"{suite.name}: cases={len(case_ids)} (min_cases={min_cases}); "
        "scoring arrives in M1, so the gate fails closed"
    )
    return 1


def main(suite: Suite, argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"python -P -m app.runners.{suite.name}")
    parser.add_argument("--dir", type=Path, default=ROOT / suite.name, help="suite directory")
    parser.add_argument("--live", action="store_true", help="record new runs (not in M0)")
    parser.add_argument("--yes", action="store_true", help="confirm a live run's cost")
    args = parser.parse_args(argv)
    if args.live and not args.yes:
        print(f"{suite.name}: --live needs --yes, after a cost estimate", file=sys.stderr)
        return 2
    if args.live:
        print(f"{suite.name}: live runs are not available in M0", file=sys.stderr)
        return 2
    if args.yes:
        print(f"{suite.name}: --yes only goes with --live", file=sys.stderr)
        return 2
    return check(suite, args.dir)
