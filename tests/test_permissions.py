"""Tests the permission gate: FORBIDDEN denies with no card, WRITE raises a
card and blocks, READ returns immediately. This is the layer between policy.py
and the browser, which the policy unit tests do not cover.
"""
import asyncio
import sys

# run from the project root: .venvScriptspython.exe -m tests.test_permissions

from app.approvals import registry
from app.permissions import Allow, Deny, make_permission_handler


class FakeSession:
    """Stands in for ChatSession: records what would be sent to the browser."""

    def __init__(self):
        self.id = "test-session"
        self.events = []

    async def emit(self, event_type, data):
        self.events.append((event_type, data))


async def main():
    failures = []

    def check(label, condition, detail=""):
        print(f"{'ok  ' if condition else 'FAIL'} {label}{(' :: ' + detail) if detail else ''}")
        if not condition:
            failures.append(label)

    # --- READ runs with no prompt and no events -----------------------------
    session = FakeSession()
    handler = make_permission_handler(session)
    result = await handler("mcp__k8s__kubectl_get", {"resourceType": "pods", "namespace": "demo"})
    check("READ allowed", isinstance(result, Allow))
    check("READ emits no event", session.events == [], str(session.events))

    # --- FORBIDDEN denies immediately, never raising a card -----------------
    session = FakeSession()
    handler = make_permission_handler(session)
    result = await asyncio.wait_for(
        handler("Bash", {"command": "rm -rf /important"}), timeout=5
    )
    check("FORBIDDEN denied", isinstance(result, Deny))
    kinds = [e[0] for e in session.events]
    check("FORBIDDEN emits policy_denial", kinds == ["policy_denial"], str(kinds))
    check("FORBIDDEN raises NO approval card", "approval_request" not in kinds)
    check(
        "denial message tells the model not to retry",
        "do not retry" in result.message.lower(),
        result.message[:80],
    )

    session = FakeSession()
    handler = make_permission_handler(session)
    result = await asyncio.wait_for(
        handler("mcp__k8s__kubectl_delete", {"resourceType": "namespace", "name": "kube-system"}),
        timeout=5,
    )
    check("kube-system delete denied", isinstance(result, Deny))
    check(
        "kube-system raises NO card",
        "approval_request" not in [e[0] for e in session.events],
    )

    # --- WRITE blocks until resolved ---------------------------------------
    session = FakeSession()
    handler = make_permission_handler(session)
    task = asyncio.create_task(
        handler("mcp__k8s__kubectl_scale",
                {"resourceType": "deployment", "name": "api", "namespace": "demo", "replicas": 3})
    )
    await asyncio.sleep(0.3)
    check("WRITE is still blocked", not task.done())
    check("WRITE raised a card", session.events and session.events[0][0] == "approval_request")

    request_id = session.events[0][1]["request_id"]
    summary = session.events[0][1]["summary"]
    check("card shows the exact command", "--replicas=3" in summary, summary)

    registry.resolve(request_id, True)
    result = await asyncio.wait_for(task, timeout=5)
    check("WRITE approved -> allow", isinstance(result, Allow))
    check(
        "resolution event sent",
        ("approval_resolved", {"request_id": request_id, "decision": "approved"}) in session.events,
    )

    # --- WRITE denied -------------------------------------------------------
    session = FakeSession()
    handler = make_permission_handler(session)
    task = asyncio.create_task(handler("Write", {"file_path": "x.yaml"}))
    await asyncio.sleep(0.3)
    registry.resolve(session.events[0][1]["request_id"], False)
    result = await asyncio.wait_for(task, timeout=5)
    check("WRITE denied -> deny", isinstance(result, Deny))
    check("denial explains", "denied" in result.message.lower())

    # --- resolving an unknown id is a no-op, not a crash -------------------
    check("unknown request id returns False", registry.resolve("nope", True) is False)

    # --- timeout denies rather than hanging forever ------------------------
    from app.config import settings
    original = settings.approval_timeout_seconds
    settings.approval_timeout_seconds = 1
    try:
        session = FakeSession()
        handler = make_permission_handler(session)
        result = await asyncio.wait_for(handler("Write", {"file_path": "y.yaml"}), timeout=8)
        check("timeout -> deny", isinstance(result, Deny))
        check(
            "timeout reported to UI",
            any(e[0] == "approval_resolved" and e[1]["decision"] == "timeout" for e in session.events),
        )
    finally:
        settings.approval_timeout_seconds = original

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all permission-wiring checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
