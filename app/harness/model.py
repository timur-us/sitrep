"""The model client the agent loop calls (ADR 0001, ADR 0003).

The Protocol below is the M1 contract; M0 declares it. No client is built and no
network library is imported in M0 (tests/test_import_allowlist.py).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class ModelClient(Protocol):
    """M1 contract:

    - One client factory builds it from settings. The direct Messages API is the default
      route; Foundry is a configuration switch in the same factory (ADR 0003). The API
      key is the only secret and is never logged.
    - create() sends one Messages API request and returns the response as a mapping. The
      tools are the registry's checked schemas (ToolRegistry.schema()), nothing else.
    - No forced tool_choice: the runtime model refuses it (ADR 0001). The loop, not the
      client, handles every stop_reason (refusal included) and the tool_result order.
    - Retries, the turn limit and the token budget (app/harness/context.py) are the
      loop's; the client does not retry on its own beyond what the loop allows.
    - Traces carry the model id, token counts, stop reason, latency and hashes, never
      prompt or completion text (CLAUDE.md rule 10, ADR 0007).
    """

    def create(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> Mapping[str, Any]: ...
