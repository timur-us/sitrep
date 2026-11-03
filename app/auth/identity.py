"""The advisor a request acts for, as the verified login established it (CLAUDE.md rule 8).

A VerifiedAdvisor is issued by app/auth/ only, through issue(): the dev shim locally
(app/auth/dev_shim.py, ADR 0009), Entra ID in Azure (M3). The login middleware attaches
it to the request's ASGI scope with attach(), and routes take it with
Depends(current_advisor). Nothing a model, a tool, a header, a query parameter or
a request body says can become one:
- the constructor refuses a call without the issuer token this module holds;
- the class is not a dataclass and has no pydantic schema, so pydantic and FastAPI
  refuse it as a field or parameter type and never build one from JSON;
- an instance cannot be changed, copied, pickled or subclassed.

Python cannot make the token unreachable, nor stop object.__setattr__ on the slot:
tests/test_identity.py scans app/ for any use of the class outside annotations,
isinstance() and `is` checks, for issue() and the token outside app/auth/, and for
object.__setattr__. app/db/session.py takes nothing but a VerifiedAdvisor.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, NoReturn

from fastapi import HTTPException, Request

_ISSUER = object()
# Where a login middleware puts the request's advisor in the ASGI scope.
_SCOPE_KEY = "app.auth.verified_advisor"


class VerifiedAdvisor:
    __slots__ = ("_advisor_id",)

    def __init__(self, advisor_id: str, issuer: object = None) -> None:
        if issuer is not _ISSUER:
            raise TypeError("a VerifiedAdvisor is issued by app/auth/ only")
        if type(advisor_id) is not str:
            raise TypeError(f"an advisor id is a str, not {type(advisor_id).__name__}")
        object.__setattr__(self, "_advisor_id", advisor_id)

    def __init_subclass__(cls, **kwargs: Any) -> NoReturn:
        raise TypeError("VerifiedAdvisor cannot be subclassed")

    @property
    def advisor_id(self) -> str:
        return self._advisor_id

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("a VerifiedAdvisor cannot be changed")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("a VerifiedAdvisor cannot be changed")

    def __reduce__(self) -> NoReturn:
        raise TypeError("a VerifiedAdvisor cannot be copied or pickled")

    def __eq__(self, other: object) -> bool:
        return type(other) is VerifiedAdvisor and other._advisor_id == self._advisor_id

    def __hash__(self) -> int:
        return hash((VerifiedAdvisor, self._advisor_id))

    def __repr__(self) -> str:
        return f"VerifiedAdvisor({self._advisor_id!r})"


def issue(advisor_id: str) -> VerifiedAdvisor:
    """For app/auth/ only: the advisor a verified login established."""
    return VerifiedAdvisor(advisor_id, _ISSUER)


def attach(scope: MutableMapping[str, Any], advisor: VerifiedAdvisor) -> None:
    """For app/auth/'s middleware: the request in scope acts for advisor.

    The ASGI scope is built by the server; no header, query parameter, cookie or body
    can put a key into it, so only a login middleware that ran for this request did.
    """
    if type(advisor) is not VerifiedAdvisor:
        raise TypeError(f"attach takes a VerifiedAdvisor, not {type(advisor).__name__}")
    scope[_SCOPE_KEY] = advisor


def current_advisor(request: Request) -> VerifiedAdvisor:
    """The route dependency: the advisor the login attached to this request, or 401.

    It reads the ASGI scope and nothing else. Headers, query parameters, cookies and
    the body are never consulted, so a request cannot name its own advisor. Without a
    login middleware (the dev shim off, M3's Entra ID not yet built) every route that
    depends on it answers 401.
    """
    advisor = request.scope.get(_SCOPE_KEY)
    if type(advisor) is not VerifiedAdvisor:
        raise HTTPException(status_code=401, detail="not signed in")
    return advisor
