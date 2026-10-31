"""The advisor a request acts for, as the verified login established it (CLAUDE.md rule 8).

A VerifiedAdvisor is issued by app/auth/ only, through issue(): the dev shim locally
(PR 7), Entra ID in Azure (M3). Nothing a model, a tool, a header, a query parameter or
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

from typing import Any, NoReturn

_ISSUER = object()


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
