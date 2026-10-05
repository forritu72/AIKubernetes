"""SSE framing, result truncation, and token/cost accounting.

The event names are unchanged from the SDK version, so `static/app.js` needs no
edits: token, thinking, tool_call, tool_result, approval_request,
approval_resolved, policy_denial, init, done, error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# What the browser is shown for one tool result.
TOOL_RESULT_LIMIT = 4000
# What actually goes back into the conversation as a tool message. This is the
# line item that used to dominate the bill: one `kubectl get pods -A` can be
# tens of thousands of tokens, re-sent on every subsequent round of the turn.
TOOL_RESULT_MODEL_LIMIT = 12_000


def sse(event_type: str, data: dict[str, Any]) -> str:
    """Encode one server-sent event frame."""
    payload = json.dumps({"type": event_type, **data}, default=str)
    return f"data: {payload}\n\n"


def truncate(text: str, limit: int = TOOL_RESULT_LIMIT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def for_model(text: str) -> str:
    """Clip a tool result before it enters the message history."""
    if len(text) <= TOOL_RESULT_MODEL_LIMIT:
        return text
    head = text[: TOOL_RESULT_MODEL_LIMIT - 1000]
    tail = text[-1000:]
    dropped = len(text) - len(head) - len(tail)
    return (
        f"{head}\n\n... [{dropped} characters omitted; narrow the query - name a "
        f"namespace, a label selector, or --tail - rather than re-running this] ...\n\n{tail}"
    )


# --- cost ------------------------------------------------------------------

# USD per 1M tokens, (input, output). Approximate and easy to edit; an unlisted
# model simply reports token counts with no dollar figure rather than a wrong one.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "o4-mini": (1.10, 4.40),
}


@dataclass
class Usage:
    """Running token total for one turn, including any subagent calls."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        self.calls += 1
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "prompt_tokens_details", None)
        self.cached_tokens += getattr(details, "cached_tokens", 0) or 0

    def cost_usd(self, model: str) -> float | None:
        prices = PRICING.get(model) or PRICING.get(model.split(":")[0])
        if prices is None:
            return None
        fresh = max(self.prompt_tokens - self.cached_tokens, 0)
        # Cached input is billed at a discount on OpenAI; 25% of list is the
        # published rate for automatic prompt caching.
        billed_input = fresh + self.cached_tokens * 0.25
        return round(
            billed_input / 1_000_000 * prices[0]
            + self.completion_tokens / 1_000_000 * prices[1],
            6,
        )
