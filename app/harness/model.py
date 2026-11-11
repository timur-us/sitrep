"""The model client the agent loop calls (ADR 0001, ADR 0003).

The Protocol below is the M1 contract; M0 declared it. M1 PR 6 builds it:
app/harness/model_client.py (ADR 0012) is the one file that imports the SDK and its HTTP
library (tests/test_import_allowlist.py).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class ModelClient(Protocol):
    """M1 contract:

    - One client factory builds it from settings. The direct Messages API is the default
      route; Foundry is a configuration switch in the same factory (ADR 0003). The
      endpoint comes from settings only, never from model or tool output. The API key is
      never logged.
    - create() sends one Messages API request and returns the response as a mapping. The
      tools are the registry's checked schemas (ToolRegistry.schema()), nothing else.
    - No forced tool_choice: the runtime model refuses it (ADR 0001). The loop, not the
      client, handles every stop_reason (refusal included) and the tool_result order.
    - Retries, the turn limit and the token budget (app/harness/context.py) are the
      loop's; the client does not retry on its own beyond what the loop allows.
    - Traces carry the model id, token counts, stop reason, latency and hashes, never
      prompt or completion text (CLAUDE.md rule 10, ADR 0007).
    - model is the id the client sends, from settings: POST /api/ask passes it to the
      loop, which refuses a reply from any other (M1 PR 12).
    """

    model: str

    def create(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> Mapping[str, Any]: ...
