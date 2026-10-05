"""Pending-approval registry: one asyncio.Future per blocked tool call."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApprovalRequest:
    id: str
    session_id: str
    tool_name: str
    summary: str
    tool_input: dict[str, Any]
    reason: str
    future: asyncio.Future[bool] = field(repr=False)

    def to_event(self) -> dict[str, Any]:
        return {
            "request_id": self.id,
            "tool": self.tool_name,
            "summary": self.summary,
            "reason": self.reason,
            "input": self.tool_input,
        }


class ApprovalRegistry:
    """Tracks approval requests awaiting a click, keyed by request id."""

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}

    def create(
        self,
        session_id: str,
        tool_name: str,
        summary: str,
        tool_input: dict[str, Any],
        reason: str,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id=uuid.uuid4().hex,
            session_id=session_id,
            tool_name=tool_name,
            summary=summary,
            tool_input=tool_input,
            reason=reason,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[request.id] = request
        return request

    def resolve(self, request_id: str, approved: bool) -> bool:
        """Settle a pending request. Returns False if it is unknown or already settled."""
        request = self._pending.pop(request_id, None)
        if request is None or request.future.done():
            return False
        request.future.set_result(approved)
        return True

    def discard(self, request_id: str) -> None:
        self._pending.pop(request_id, None)

    def pending_for(self, session_id: str) -> list[ApprovalRequest]:
        return [r for r in self._pending.values() if r.session_id == session_id]

    def deny_all(self, session_id: str) -> None:
        """Deny every outstanding request for a session (used on interrupt/teardown)."""
        for request in self.pending_for(session_id):
            self._pending.pop(request.id, None)
            if not request.future.done():
                request.future.set_result(False)


registry = ApprovalRegistry()
