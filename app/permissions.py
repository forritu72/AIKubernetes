"""The permission gate: policy decision -> auto-run, approval card, or deny.

The agent loop calls this before every tool call. It replaces the Claude Agent
SDK's `can_use_tool` callback; the contract is the same, minus the SDK types,
and `app/policy.py` is untouched.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.approvals import registry
from app.config import settings
from app.policy import Bucket, classify

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Allow:
    """The tool call may run."""


@dataclass(frozen=True)
class Deny:
    """The tool call must not run; `message` is fed back to the model as the result."""

    message: str


PermissionResult = Allow | Deny


class EventSink(Protocol):
    """The bit of a chat session the permission layer needs: an id and a way to
    push an event to the browser."""

    id: str

    async def emit(self, event_type: str, data: dict[str, Any]) -> None: ...


def make_permission_handler(session: EventSink):
    """Build a permission callback bound to one chat session."""

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any]
    ) -> PermissionResult:
        decision = classify(tool_name, tool_input)

        if decision.bucket is Bucket.FORBIDDEN:
            log.warning("DENY (policy) %s: %s", tool_name, decision.reason)
            await session.emit(
                "policy_denial",
                {"tool": tool_name, "summary": decision.summary, "reason": decision.reason},
            )
            # The message is fed back to the model so it explains the block
            # instead of silently retrying a variant.
            return Deny(
                f"{decision.reason} This is a hard policy limit in the DevOps "
                "Copilot, not a missing permission - do not retry a variation of "
                "this call. Tell the user what was blocked and why."
            )

        if decision.bucket is Bucket.READ:
            return Allow()

        # WRITE: ask the user, block until they click.
        request = registry.create(
            session_id=session.id,
            tool_name=tool_name,
            summary=decision.summary,
            tool_input=tool_input,
            reason=decision.reason,
        )
        await session.emit("approval_request", request.to_event())
        log.debug("approval request %s for %s", request.id, tool_name)

        try:
            approved = await asyncio.wait_for(
                request.future, timeout=settings.approval_timeout_seconds
            )
        except asyncio.TimeoutError:
            registry.discard(request.id)
            await session.emit(
                "approval_resolved",
                {"request_id": request.id, "decision": "timeout"},
            )
            return Deny(
                "The approval request timed out with no response. Stop and wait "
                "for the user rather than retrying."
            )
        except asyncio.CancelledError:
            registry.discard(request.id)
            raise

        await session.emit(
            "approval_resolved",
            {
                "request_id": request.id,
                "decision": "approved" if approved else "denied",
            },
        )

        if approved:
            return Allow()
        return Deny(
            "The user denied this action. Do not retry it or work around it with "
            "a different tool. Acknowledge the denial and ask what they would "
            "prefer instead."
        )

    return can_use_tool
