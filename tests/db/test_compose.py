"""compose.yaml, read statically: one service, published on 127.0.0.1 only, trust auth on
that service only, the image pinned by digest, init mounted read-only, and nothing
taken from the environment (no `${…}`, no env_file).
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
import yaml
from repo_files import ROOT

from app.db import names

COMPOSE = ROOT / "compose.yaml"
IMAGE = re.compile(r"pgvector/pgvector:\d+\.\d+\.\d+-pg17-bookworm@sha256:[0-9a-f]{64}")
INIT_MOUNT = "./db/init:/docker-entrypoint-initdb.d:ro"
TRUST = ("POSTGRES_HOST_AUTH_METHOD", "trust")


def _environment(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {str(key): str(value) for key, value in env.items()}


def _keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [str(key) for key in node] + [key for value in node.values() for key in _keys(value)]
    if isinstance(node, list):
        return [key for item in node for key in _keys(item)]
    return []


def _port_problems(name: str, ports: Any) -> list[str]:
    found = []
    for entry in ports or []:
        if isinstance(entry, dict):
            if entry.get("host_ip") != "127.0.0.1":
                found.append(f"{name}: port {entry!r} is not on host_ip 127.0.0.1")
        elif not str(entry).startswith("127.0.0.1:"):
            found.append(f"{name}: port {entry!r} does not start with 127.0.0.1:")
    return found


def problems(compose: dict[str, Any], raw: str) -> list[str]:
    found = []
    services = compose.get("services") or {}
    if set(services) != {"db"}:
        found.append(f"services {sorted(services)}, expected ['db']")
    for name, service in services.items():
        found += _port_problems(name, service.get("ports"))
        if "network_mode" in service:
            found.append(f"{name}: network_mode {service['network_mode']!r}")
        if TRUST in _environment(service).items() and name != "db":
            found.append(f"{name}: trust auth outside db")
    db = services.get("db") or {}
    if not IMAGE.fullmatch(str(db.get("image", ""))):
        found.append(f"db: image {db.get('image')!r} is not pgvector on pg17 pinned by digest")
    if db.get("volumes") != [INIT_MOUNT]:
        found.append(f"db: volumes {db.get('volumes')!r}, expected [{INIT_MOUNT!r}]")
    if not db.get("ports"):
        found.append("db: no ports")
    if "env_file" in _keys(compose):
        found.append("env_file is set")
    if "${" in raw or re.search(r"\$[A-Za-z_]", raw):
        found.append("a variable is interpolated")
    return found


def _real() -> tuple[dict[str, Any], str]:
    raw = COMPOSE.read_text(encoding="utf-8")
    return yaml.safe_load(raw), raw


def test_compose() -> None:
    compose, raw = _real()
    found = problems(compose, raw)
    assert not found, "compose.yaml:\n" + "\n".join(found)
    db = compose["services"]["db"]
    # Non-vacuity: the port, the trust setting and the healthcheck were there to check.
    assert db["ports"] == ["127.0.0.1:55432:5432"], db["ports"]
    assert TRUST in _environment(db).items(), db.get("environment")
    # The healthcheck logs in over TCP as the app role, so "healthy" means the roles exist.
    healthcheck = db["healthcheck"]["test"]
    expected = ["CMD", "psql", "-h", "127.0.0.1", "-U", names.APP_ROLE, "-d", names.DATABASE]
    assert healthcheck[: len(expected)] == expected, healthcheck


def _planted(change: Any) -> list[str]:
    compose, raw = _real()
    compose = copy.deepcopy(compose)
    change(compose)
    return problems(compose, raw)


PLANTED = [
    (lambda c: c["services"]["db"].update(ports=["0.0.0.0:55432:5432"]), "127.0.0.1:"),
    (lambda c: c["services"]["db"].update(ports=["55432:5432"]), "127.0.0.1:"),
    (lambda c: c["services"]["db"].update(ports=["5432"]), "127.0.0.1:"),
    (lambda c: c["services"]["db"].update(ports=["[::]:55432:5432"]), "127.0.0.1:"),
    (lambda c: c["services"]["db"].update(ports=["127.0.0.10:55432:5432"]), "127.0.0.1:"),
    (lambda c: c["services"]["db"].update(ports=[{"target": 5432, "published": 55432}]), "host_ip"),
    (lambda c: c["services"]["db"].update(network_mode="host"), "network_mode"),
    (lambda c: c["services"]["db"].update(env_file=[".env"]), "env_file"),
    (lambda c: c["services"]["db"].update(image="pgvector/pgvector:0.8.7-pg17-bookworm"), "image"),
    (
        lambda c: c["services"]["db"].update(volumes=["./db/init:/docker-entrypoint-initdb.d"]),
        "volumes",
    ),
    (
        lambda c: c["services"].update(
            app={"image": "x", "environment": ["POSTGRES_HOST_AUTH_METHOD=trust"]}
        ),
        "trust auth outside db",
    ),
    (lambda c: c["services"].update(app={"image": "x"}), "services"),
]


@pytest.mark.parametrize(("change", "needle"), PLANTED)
def test_planted_compose_files_are_caught(change: Any, needle: str) -> None:
    found = _planted(change)
    assert any(needle in line for line in found), found


@pytest.mark.parametrize("text", ["image: ${IMAGE}\n", 'ports: ["$PORT:5432"]\n'])
def test_planted_interpolation_is_caught(text: str) -> None:
    compose, raw = _real()
    assert "a variable is interpolated" in problems(compose, raw + text)
