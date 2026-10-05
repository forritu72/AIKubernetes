"""Agent-loop tests. Run: .venv\\Scripts\\python.exe -m tests.test_agent_loop

No pytest and no network: the OpenAI client is replaced by a scripted stub that
replays streaming chunks, so this exercises the real loop - tool-call
accumulation across deltas, the permission gate, subagent dispatch, history
shape, and the SSE frames the browser receives.

The history assertions matter more than they look: the Chat Completions API
rejects an assistant message carrying `tool_calls` that is not followed by one
`tool` message per id, and that is exactly what naive history trimming breaks.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

from app.agent import ChatSession, ToolBox
from app.approvals import registry
from app.config import clusters, settings

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"{'ok  ' if condition else 'FAIL'} {label}{(' :: ' + str(detail)[:100]) if detail else ''}")
    if not condition:
        FAILURES.append(label)


# --- the scripted model -----------------------------------------------------

def text_turn(text: str) -> list[SimpleNamespace]:
    """One assistant answer, streamed a few characters at a time."""
    chunks = []
    for i in range(0, len(text), 8):
        chunks.append(_chunk(content=text[i : i + 8]))
    return chunks


def tool_turn(name: str, arguments: dict, call_id: str = "call_1") -> list[SimpleNamespace]:
    """One tool call, with the name and arguments split across deltas the way
    the real API sends them."""
    blob = json.dumps(arguments)
    parts = [blob[: len(blob) // 2], blob[len(blob) // 2 :]]
    return [
        _chunk(tool_call=(0, call_id, name, "")),
        _chunk(tool_call=(0, None, None, parts[0])),
        _chunk(tool_call=(0, None, None, parts[1])),
    ]


def _chunk(content: str | None = None, tool_call: tuple | None = None) -> SimpleNamespace:
    calls = None
    if tool_call is not None:
        index, call_id, name, args = tool_call
        calls = [
            SimpleNamespace(
                index=index,
                id=call_id,
                function=SimpleNamespace(name=name, arguments=args),
            )
        ]
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=calls))],
    )


USAGE = SimpleNamespace(
    usage=SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    ),
    choices=[],
)


class ScriptedClient:
    """Stands in for AsyncOpenAI. Each entry in `script` is one model call."""

    def __init__(self, script: list[list[SimpleNamespace]]) -> None:
        self.script = script
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        # Snapshot: the loop mutates `messages` in place (that is how the
        # orchestrator's history persists), so storing the reference would show
        # every past request the final transcript.
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        chunks = self.script.pop(0) if self.script else text_turn("done.")

        async def stream():
            for chunk in chunks:
                yield chunk
            yield USAGE

        return stream()

    async def close(self) -> None:
        pass


class FakeHub:
    """An MCP hub with no servers behind it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._tools = {
            "mcp__k8s__kubectl_get": "NAME READY STATUS\napi-0 0/1 CrashLoopBackOff",
            "mcp__k8s__kubectl_logs": "panic: missing DATABASE_URL",
            "mcp__k8s__kubectl_describe": "Events: Back-off restarting failed container",
            "mcp__k8s__kubectl_scale": "deployment.apps/api scaled",
            "mcp__k8s__explain_resource": "KIND: Deployment",
        }

    def schemas(self):
        return [
            {
                "type": "function",
                "function": {"name": n, "description": n, "parameters": {"type": "object", "properties": {}}},
            }
            for n in self._tools
        ]

    def knows(self, name: str) -> bool:
        return name in self._tools

    async def call(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        return self._tools[name]

    async def aclose(self) -> None:
        pass

    @property
    def status(self):
        return [{"name": "k8s", "status": "connected", "error": None, "tools": len(self._tools)}]


async def make_session(script: list[list[SimpleNamespace]]) -> tuple[ChatSession, ScriptedClient, FakeHub]:
    session = ChatSession("test", clusters.active)
    hub = FakeHub()
    client = ScriptedClient(script)
    session.hub = hub            # skip start(): no real servers, no real key
    session.toolbox = ToolBox(hub)
    session._client = client
    session.mcp_status = hub.status
    session.history = [{"role": "system", "content": "test system prompt"}]
    return session, client, hub


async def drive(session: ChatSession, prompt: str, *, approve: bool = True) -> list[dict]:
    """Consume a turn, auto-answering any approval card."""
    events = []
    async for frame in session.run_turn(prompt):
        event = json.loads(frame.removeprefix("data: ").strip())
        events.append(event)
        if event["type"] == "approval_request":
            registry.resolve(event["request_id"], approve)
    return events


def kinds(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# --- the tests --------------------------------------------------------------

async def test_read_runs_without_a_card():
    session, client, hub = await make_session([
        tool_turn("mcp__k8s__kubectl_get", {"resourceType": "pods", "namespace": "demo"}),
        text_turn("api-0 is in CrashLoopBackOff."),
    ])
    events = await drive(session, "what is broken in demo?")
    seen = kinds(events)

    check("READ: tool actually ran", hub.calls and hub.calls[0][0] == "mcp__k8s__kubectl_get", hub.calls)
    check("READ: arguments survived split deltas", hub.calls[0][1] == {"resourceType": "pods", "namespace": "demo"}, hub.calls[0][1])
    check("READ: no approval card", "approval_request" not in seen, seen)
    check("READ: tool_call and tool_result emitted", "tool_call" in seen and "tool_result" in seen, seen)
    check("READ: answer streamed as tokens", "".join(e["text"] for e in events if e["type"] == "token") == "api-0 is in CrashLoopBackOff.")
    done = events[-1]
    check("READ: done is last", done["type"] == "done" and not done["is_error"], done.get("subtype"))
    check("READ: token usage reported", done["input_tokens"] == 200 and done["output_tokens"] == 40, done)
    check("READ: cost computed", isinstance(done["total_cost_usd"], float), done["total_cost_usd"])

    # history must be a valid Chat Completions transcript
    roles = [m["role"] for m in session.history]
    check("READ: history shape", roles == ["system", "user", "assistant", "tool", "assistant"], roles)
    assistant = session.history[2]
    tool_msg = session.history[3]
    check("READ: tool result answers the call id", tool_msg["tool_call_id"] == assistant["tool_calls"][0]["id"], tool_msg)
    check("READ: second request carried the tool result", len(client.requests[1]["messages"]) == 4, len(client.requests[1]["messages"]))


async def test_write_blocks_on_approval():
    session, _, hub = await make_session([
        tool_turn("mcp__k8s__kubectl_scale", {"resourceType": "deployment", "name": "api", "namespace": "demo", "replicas": 3}),
        text_turn("Scaled api to 3 replicas."),
    ])
    events = await drive(session, "scale api to 3", approve=True)
    seen = kinds(events)

    check("WRITE: raised an approval card", "approval_request" in seen, seen)
    card = next(e for e in events if e["type"] == "approval_request")
    check("WRITE: card shows the exact command", "--replicas=3" in card["summary"], card["summary"])
    check("WRITE: resolution reported", any(e["type"] == "approval_resolved" and e["decision"] == "approved" for e in events))
    check("WRITE: approved call ran", any(c[0] == "mcp__k8s__kubectl_scale" for c in hub.calls), hub.calls)


async def test_denied_write_never_runs():
    session, _, hub = await make_session([
        tool_turn("mcp__k8s__kubectl_scale", {"resourceType": "deployment", "name": "api", "namespace": "demo", "replicas": 0}),
        text_turn("Understood, I will not scale it."),
    ])
    events = await drive(session, "scale api to 0", approve=False)

    check("DENY: nothing was executed", hub.calls == [], hub.calls)
    result = next(e for e in events if e["type"] == "tool_result")
    check("DENY: model told not to retry", "do not retry" in result["content"].lower(), result["content"][:70])
    check("DENY: flagged as an error result", result["is_error"] is True)


async def test_forbidden_is_blocked_before_execution():
    session, _, hub = await make_session([
        tool_turn("Bash", {"command": "rm -rf /important"}),
        text_turn("That is blocked by policy."),
    ])
    events = await drive(session, "clean up disk")
    seen = kinds(events)

    check("FORBIDDEN: policy_denial emitted", "policy_denial" in seen, seen)
    check("FORBIDDEN: no approval card", "approval_request" not in seen, seen)
    check("FORBIDDEN: command never ran", hub.calls == [], hub.calls)


async def test_malformed_arguments_do_not_crash():
    broken = [
        _chunk(tool_call=(0, "call_x", "mcp__k8s__kubectl_get", "{not json")),
    ]
    session, _, hub = await make_session([broken, text_turn("Retrying properly.")])
    events = await drive(session, "get pods")

    check("BAD ARGS: turn survived", events[-1]["type"] == "done" and not events[-1]["is_error"], events[-1])
    check("BAD ARGS: nothing executed", hub.calls == [], hub.calls)
    result = next(e for e in events if e["type"] == "tool_result")
    check("BAD ARGS: model told what was wrong", "parse" in result["content"].lower(), result["content"][:70])


async def test_subagent_runs_in_its_own_context():
    session, client, hub = await make_session([
        # orchestrator delegates
        tool_turn("Task", {"subagent_type": "k8s-diagnostician", "description": "triage api",
                           "prompt": "Find out why api-0 in demo is crashlooping."}),
        # subagent: one read, then its report
        tool_turn("mcp__k8s__kubectl_logs", {"name": "api-0", "namespace": "demo", "previous": True}, call_id="call_sub"),
        text_turn("Root cause: DATABASE_URL is unset."),
        # orchestrator summarises
        text_turn("The app is missing DATABASE_URL."),
    ])
    events = await drive(session, "why is api broken?")
    seen = kinds(events)

    check("TASK: subagent's tool ran", any(c[0] == "mcp__k8s__kubectl_logs" for c in hub.calls), hub.calls)
    sub_calls = [e for e in events if e["type"] == "tool_call" and e.get("subagent")]
    check("TASK: subagent calls flagged for the UI", len(sub_calls) == 1, sub_calls)
    check("TASK: subagent text not streamed as tokens",
          "".join(e["text"] for e in events if e["type"] == "token") == "The app is missing DATABASE_URL.")

    # the subagent's request must carry its own system prompt and a narrow toolset
    sub_request = client.requests[1]
    check("TASK: subagent got its own system prompt",
          sub_request["messages"][0]["content"].startswith("You investigate broken Kubernetes"),
          sub_request["messages"][0]["content"][:40])
    check("TASK: subagent cannot see the conversation", len(sub_request["messages"]) == 2, len(sub_request["messages"]))
    offered = {t["function"]["name"] for t in sub_request["tools"]}
    check("TASK: subagent toolset is restricted", "mcp__k8s__kubectl_scale" not in offered and "Write" not in offered, sorted(offered))
    check("TASK: subagent cannot delegate further", "Task" not in offered, sorted(offered))

    # only the report comes back into the orchestrator's history
    tool_msg = next(m for m in session.history if m["role"] == "tool")
    check("TASK: only the report returns", tool_msg["content"] == "Root cause: DATABASE_URL is unset.", tool_msg["content"])


async def test_round_limit_stops_the_loop():
    original = settings.max_tool_rounds
    settings.max_tool_rounds = 3
    try:
        session, client, hub = await make_session(
            [tool_turn("mcp__k8s__kubectl_get", {"resourceType": "pods"}, call_id=f"c{i}") for i in range(10)]
            + [text_turn("Stopping: I hit the round limit.")]
        )
        events = await drive(session, "loop forever")
        done = events[-1]
        check("LIMIT: loop stopped at the cap", done["num_turns"] == 3, done["num_turns"])
        check("LIMIT: model asked to summarise", client.requests[-1]["messages"][-1]["content"].startswith("You have used all 3"),
              client.requests[-1]["messages"][-1]["content"][:40])
        check("LIMIT: no tools offered on the final call", not client.requests[-1].get("tools"))
        check("LIMIT: turn ends cleanly", done["type"] == "done" and not done["is_error"])
    finally:
        settings.max_tool_rounds = original


async def test_history_trimming_keeps_a_valid_transcript():
    session, _, _ = await make_session([text_turn("ok")])
    session.history = [{"role": "system", "content": "sys"}]
    for i in range(30):
        session.history += [
            {"role": "user", "content": f"question {i} " + "x" * 4000},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "mcp__k8s__kubectl_get", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 4000},
            {"role": "assistant", "content": f"answer {i}"},
        ]
    before = len(session.history)
    session._trim_history()
    after = session.history

    check("TRIM: something was dropped", len(after) < before, f"{before} -> {len(after)}")
    check("TRIM: system prompt kept", after[0]["role"] == "system")
    check("TRIM: cut at a user boundary", after[1]["role"] == "user", after[1]["role"])

    # every tool_calls id must still be answered, or the API 400s
    open_ids: list[str] = []
    valid = True
    for message in after:
        if message["role"] == "assistant" and message.get("tool_calls"):
            open_ids = [c["id"] for c in message["tool_calls"]]
        elif message["role"] == "tool":
            if message["tool_call_id"] not in open_ids:
                valid = False
            else:
                open_ids.remove(message["tool_call_id"])
        elif open_ids:
            valid = False
    check("TRIM: no orphaned tool messages", valid and not open_ids, after[:2])


async def test_second_turn_reuses_history():
    session, client, _ = await make_session([text_turn("first answer"), text_turn("second answer")])
    await drive(session, "first question")
    await drive(session, "second question")
    roles = [m["role"] for m in session.history]
    check("MULTI-TURN: history accumulates", roles == ["system", "user", "assistant", "user", "assistant"], roles)
    check("MULTI-TURN: second call saw the first exchange", len(client.requests[1]["messages"]) == 4, len(client.requests[1]["messages"]))


TESTS = [
    test_read_runs_without_a_card,
    test_write_blocks_on_approval,
    test_denied_write_never_runs,
    test_forbidden_is_blocked_before_execution,
    test_malformed_arguments_do_not_crash,
    test_subagent_runs_in_its_own_context,
    test_round_limit_stops_the_loop,
    test_history_trimming_keeps_a_valid_transcript,
    test_second_turn_reuses_history,
]


async def main() -> int:
    for test in TESTS:
        print(f"\n--- {test.__name__} ---")
        await test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print("  ", failure)
        return 1
    print("all agent-loop checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
