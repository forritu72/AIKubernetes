"""The agent loop, over any OpenAI-compatible Chat Completions endpoint.

This is the replacement for the Claude Agent SDK: the loop, the tool registry,
subagent dispatch, streaming, and history management are all here. The MCP
wiring lives in `app/mcp_client.py`, the local tools in `app/builtin_tools.py`,
and the safety model is unchanged - every tool call still goes through
`app/permissions.py` before it runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Iterable

from openai import AsyncOpenAI

from app import builtin_tools
from app.approvals import registry
from app.builtin_tools import ToolError
from app.config import PROJECT_ROOT, WORKSPACE, ClusterProfile, clusters, settings
from app.events import Usage, for_model, sse, truncate
from app.mcp_client import MCPHub
from app.mcp_config import build_mcp_servers
from app.permissions import Allow, Deny, make_permission_handler
from app.policy import classify, summarize
from app.prompts import DEVOPS_PROMPT, ORCHESTRATOR_GITHUB_TOOLS, SUBAGENTS
from app import trace

log = logging.getLogger(__name__)

_DONE = object()
TASK_TOOL = "Task"


def _client() -> AsyncOpenAI:
    if not settings.has_llm_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set - add it to .env (or set OPENAI_BASE_URL "
            "to a local endpoint that needs no key)."
        )
    return AsyncOpenAI(
        # A local endpoint still wants a non-empty string; it is ignored there.
        api_key=settings.openai_api_key.strip() or "not-needed",
        base_url=settings.openai_base_url.strip() or None,
        max_retries=3,
    )


def _task_schema() -> dict[str, Any]:
    roster = "\n".join(f"- {name}: {a.description}" for name, a in SUBAGENTS.items())
    return {
        "type": "function",
        "function": {
            "name": TASK_TOOL,
            "description": (
                "Delegate a self-contained piece of work to a specialist subagent "
                "and get back its report. The subagent has its own context and "
                "cannot see this conversation, so the prompt must stand alone.\n\n"
                f"Available subagents:\n{roster}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subagent_type": {
                        "type": "string",
                        "enum": sorted(SUBAGENTS),
                        "description": "Which specialist to run.",
                    },
                    "description": {
                        "type": "string",
                        "description": "A few words naming the task, shown to the user.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The complete instruction for the subagent, including "
                            "every name, namespace, and repo it needs."
                        ),
                    },
                },
                "required": ["subagent_type", "prompt"],
            },
        },
    }


class ToolBox:
    """Every tool available to one session: MCP tools plus the local built-ins."""

    def __init__(self, hub: MCPHub) -> None:
        self.hub = hub
        self._schemas: dict[str, dict[str, Any]] = {}
        for schema in hub.schemas():
            self._schemas[schema["function"]["name"]] = schema
        self._schemas.update(builtin_tools.SCHEMAS)
        self._schemas[TASK_TOOL] = _task_schema()

    def schemas(self, allowed: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Tool definitions to send, optionally narrowed to a subagent's set.

        A name in `allowed` that no connected server provides is skipped rather
        than faked, so a subagent whose MCP server failed to start gets a
        smaller toolset instead of tools that error on every call.
        """
        if allowed is None:
            return list(self._schemas.values())
        return [self._schemas[name] for name in allowed if name in self._schemas]

    def orchestrator_names(self) -> list[str]:
        """What the top-level agent is offered.

        Everything except the bulk of the GitHub server, whose schemas would
        otherwise be the single largest line item in every request. Subagents
        still reach the full set through `schemas(allowed)`.
        """
        return [
            name
            for name in self._schemas
            if not name.startswith("mcp__github__") or name in ORCHESTRATOR_GITHUB_TOOLS
        ]

    def knows(self, name: str) -> bool:
        return name in self._schemas

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name in builtin_tools.NAMES:
            return await builtin_tools.call(name, arguments)
        if self.hub.knows(name):
            return await self.hub.call(name, arguments)
        raise ToolError(f"unknown tool: {name}")


class ChatSession:
    """One browser chat: MCP servers, message history, an event queue, a turn lock."""

    def __init__(self, session_id: str, profile: ClusterProfile) -> None:
        self.id = session_id
        self.profile = profile
        self.hub: MCPHub | None = None
        self.toolbox: ToolBox | None = None
        self.history: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.turn_lock = asyncio.Lock()
        self.mcp_status: list[dict[str, Any]] = []
        self.gate = make_permission_handler(self)
        self._client: AsyncOpenAI | None = None
        self._pump: asyncio.Task[None] | None = None
        self._announced = False

    # --- EventSink protocol (used by permissions.py) ------------------------

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        await self.queue.put((event_type, data))

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self.hub is not None:
            return
        self._client = _client()
        hub = MCPHub(build_mcp_servers(self.profile))
        await hub.connect()
        self.hub = hub
        self.toolbox = ToolBox(hub)
        self.mcp_status = hub.status
        self.history = [{"role": "system", "content": DEVOPS_PROMPT}]
        log.info(
            "session %s ready (cluster=%s, %d tools)",
            self.id,
            self.profile.name,
            len(self.toolbox.schemas()),
        )

    async def close(self) -> None:
        registry.deny_all(self.id)
        if self._pump and not self._pump.done():
            self._pump.cancel()
        if self.hub is not None:
            try:
                await self.hub.aclose()
            except Exception as exc:
                log.warning("shutdown failed for %s: %s", self.id, exc)
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        self.hub = None
        self.toolbox = None
        self._client = None
        self._announced = False

    async def interrupt(self) -> None:
        registry.deny_all(self.id)
        if self._pump and not self._pump.done():
            self._pump.cancel()

    # --- workspace awareness ------------------------------------------------

    def _workspace_inventory(self) -> str:
        """A listing of the files the agent has already written.

        Without this the agent has no idea its own manifests exist and asks the
        user to supply a file it wrote ten minutes earlier - it cannot see the
        disk unless it calls Glob, and nothing prompts it to. Regenerated per
        turn but only written back when it changes, so a steady workspace keeps
        the system prompt byte-identical and the prompt cache intact.
        """
        root = WORKSPACE
        if not root.is_dir():
            return ""
        entries: list[str] = []
        for path in sorted(root.rglob("*")):
            if path.is_dir() or any(part.startswith(".") for part in path.parts):
                continue
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            entries.append(f"- {rel} ({path.stat().st_size} bytes)")
            if len(entries) >= 60:
                entries.append("- ... (more; use Glob to list the rest)")
                break
        header = "\n\n## Files on disk\n\n"
        if not entries:
            return (
                header + "The workspace is empty. Write new manifests to "
                "`workspace/manifests/<name>.yaml`."
            )
        return (
            header
            + "These already exist. Read one with `Read` before asking the user "
            "for anything, and never ask them to paste or upload a file that is "
            "listed here - you wrote it, you can open it:\n\n"
            + "\n".join(entries)
        )

    def _refresh_system_prompt(self) -> None:
        content = DEVOPS_PROMPT + self._workspace_inventory()
        if self.history and self.history[0].get("role") == "system":
            if self.history[0]["content"] != content:
                self.history[0]["content"] = content
                log.debug("session %s: workspace inventory changed", self.id)
        else:
            self.history.insert(0, {"role": "system", "content": content})

    # --- history ------------------------------------------------------------

    def _trim_history(self) -> None:
        """Drop the oldest complete exchanges once the history gets expensive.

        Cuts only at a `user` message: an assistant message carrying tool_calls
        and the `tool` messages answering it must stay together or the API
        rejects the request outright.
        """
        budget = max(settings.history_token_budget, 4000)
        # ~4 characters per token is close enough to decide when to cut, and
        # avoids pulling in a tokenizer just for this.
        def size(messages: list[dict[str, Any]]) -> int:
            return sum(len(json.dumps(m, default=str)) for m in messages) // 4

        if size(self.history) <= budget:
            return

        system, rest = self.history[:1], self.history[1:]
        while size(system + rest) > budget:
            cut = next(
                (i for i, m in enumerate(rest[1:], start=1) if m.get("role") == "user"),
                None,
            )
            if cut is None:
                break
            rest = rest[cut:]
        dropped = len(self.history) - len(system + rest)
        if dropped:
            log.info("session %s: trimmed %d old messages", self.id, dropped)
        self.history = system + rest

    # --- one turn -----------------------------------------------------------

    async def run_turn(self, prompt: str) -> AsyncIterator[str]:
        """Send one user message and yield SSE frames until the turn ends."""
        if self.turn_lock.locked():
            yield sse("error", {"message": "A turn is already running in this session."})
            return

        async with self.turn_lock:
            try:
                await self.start()
            except Exception as exc:
                yield sse("error", {"message": f"{type(exc).__name__}: {exc}"})
                return

            while not self.queue.empty():          # drop anything stale
                self.queue.get_nowait()

            if not self._announced:
                self._announced = True
                assert self.toolbox is not None
                await self.emit(
                    "init",
                    {
                        "model": settings.agent_model,
                        "session_id": self.id,
                        "mcp_servers": self.mcp_status,
                        "tools": len(self.toolbox.schemas()),
                    },
                )

            self._pump = asyncio.create_task(self._drive(prompt))
            try:
                while True:
                    item = await self.queue.get()
                    if item is _DONE:
                        break
                    event_type, data = item
                    yield sse(event_type, data)
            except asyncio.CancelledError:
                # Browser went away mid-turn: stop the agent rather than
                # leaving it running against the cluster unattended.
                if self._pump and not self._pump.done():
                    self._pump.cancel()
                registry.deny_all(self.id)
                raise
            finally:
                self._pump = None

    async def _drive(self, prompt: str) -> None:
        """Run the orchestrator loop, funnelling everything onto the queue."""
        started = time.monotonic()
        usage = Usage()
        subtype, is_error, rounds, final = "success", False, 0, ""
        trace.turn_start(self.id, prompt)
        try:
            self._refresh_system_prompt()
            self.history.append({"role": "user", "content": prompt})
            self._trim_history()
            final, rounds = await self._run_agent(
                messages=self.history,
                model=settings.agent_model,
                tool_names=self.toolbox.orchestrator_names() if self.toolbox else None,
                max_rounds=settings.max_tool_rounds,
                usage=usage,
                stream_text=True,
            )
        except asyncio.CancelledError:
            # The queue is about to stop being read; _DONE just unblocks the
            # generator if it is still alive.
            await self.queue.put(("error", {"message": "Interrupted."}))
            await self.queue.put(_DONE)
            raise
        except Exception as exc:
            log.exception("turn failed in session %s", self.id)
            subtype, is_error = type(exc).__name__, True
            await self.queue.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))

        trace.turn_end(
            time.monotonic() - started, rounds, usage, usage.cost_usd(settings.agent_model)
        )
        await self.queue.put(
            (
                "done",
                {
                    "subtype": subtype,
                    "is_error": is_error,
                    "num_turns": rounds,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "total_cost_usd": usage.cost_usd(settings.agent_model),
                    "input_tokens": usage.prompt_tokens,
                    "output_tokens": usage.completion_tokens,
                    "cached_tokens": usage.cached_tokens,
                    "session_id": self.id,
                    "result": final,
                },
            )
        )
        await self.queue.put(_DONE)

    # --- the loop itself ----------------------------------------------------

    async def _run_agent(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        tool_names: Iterable[str] | None,
        max_rounds: int,
        usage: Usage,
        stream_text: bool,
        depth: int = 0,
    ) -> tuple[str, int]:
        """Model -> tools -> model, until the model stops calling tools.

        Returns the final assistant text and how many model calls it took.
        `messages` is mutated in place, which is what lets the orchestrator's
        history persist across turns while a subagent's stays local.
        """
        assert self.toolbox is not None and self._client is not None
        tools = self.toolbox.schemas(tool_names)
        text = ""

        for round_number in range(1, max_rounds + 1):
            text, calls = await self._complete(
                messages=messages,
                model=model,
                tools=tools,
                usage=usage,
                stream_text=stream_text,
                depth=depth,
            )

            assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                assistant["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["args"]},
                    }
                    for call in calls
                ]
            messages.append(assistant)

            if not calls:
                return text, round_number

            for call in calls:
                result, failed = await self._invoke(call, depth=depth, usage=usage)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": for_model(result) or ("error" if failed else "(no output)"),
                    }
                )

        messages.append(
            {
                "role": "user",
                "content": (
                    f"You have used all {max_rounds} tool rounds for this request. "
                    "Stop calling tools and summarise what you found, what you "
                    "changed, and what is still outstanding."
                ),
            }
        )
        text, _ = await self._complete(
            messages=messages, model=model, tools=[], usage=usage,
            stream_text=stream_text, depth=depth,
        )
        messages.append({"role": "assistant", "content": text})
        return text, max_rounds

    async def _complete(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]],
        usage: Usage,
        stream_text: bool,
        depth: int = 0,
    ) -> tuple[str, list[dict[str, str]]]:
        """One streamed model call. Returns (text, accumulated tool calls)."""
        assert self._client is not None
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": settings.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            # One call at a time keeps approval cards in a sane order and stops
            # the model firing a mutation alongside the read that justifies it.
            kwargs["parallel_tool_calls"] = False

        trace.model_start(model, len(messages), len(tools), depth=depth)
        began = time.monotonic()
        stream = await self._client.chat.completions.create(**kwargs)

        parts: list[str] = []
        slots: dict[int, dict[str, str]] = {}
        last_usage = None
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                last_usage = chunk.usage
                usage.add(chunk.usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                parts.append(delta.content)
                if stream_text:
                    await self.emit("token", {"text": delta.content})
            for piece in delta.tool_calls or []:
                slot = slots.setdefault(piece.index, {"id": "", "name": "", "args": ""})
                if piece.id:
                    slot["id"] = piece.id
                if piece.function:
                    if piece.function.name:
                        slot["name"] += piece.function.name
                    if piece.function.arguments:
                        slot["args"] += piece.function.arguments

        calls = [slots[i] for i in sorted(slots) if slots[i]["name"]]
        for call in calls:
            if not call["id"]:
                call["id"] = f"call_{uuid.uuid4().hex[:20]}"
        trace.model_end(model, time.monotonic() - began, last_usage, len(calls), depth=depth)
        return "".join(parts), calls

    async def _invoke(
        self, call: dict[str, str], *, depth: int, usage: Usage
    ) -> tuple[str, bool]:
        """Gate one tool call, run it if allowed, and report it to the browser."""
        name = call["name"]
        try:
            arguments = json.loads(call["args"]) if call["args"].strip() else {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            # A malformed argument blob never reaches the cluster; tell the
            # model precisely what was wrong so it can retry the call properly.
            await self.emit(
                "tool_call",
                {
                    "id": call["id"],
                    "tool": name,
                    "summary": f"{name} (malformed arguments)",
                    "input": {"raw": call["args"][:500]},
                    "subagent": depth > 0,
                },
            )
            message = f"Could not parse the arguments as JSON: {exc}. Call the tool again."
            await self.emit(
                "tool_result",
                {"id": call["id"], "content": message, "truncated": False, "is_error": True},
            )
            return message, True

        if not self.toolbox or not (self.toolbox.knows(name) or name == TASK_TOOL):
            message = f"No such tool: {name}."
            await self.emit(
                "tool_result",
                {"id": call["id"], "content": message, "truncated": False, "is_error": True},
            )
            return message, True

        summary = _summary(name, arguments)
        bucket = classify(name, arguments).bucket.value
        trace.tool_start(name, summary, bucket, arguments, subagent=depth > 0)

        await self.emit(
            "tool_call",
            {
                "id": call["id"],
                "tool": name,
                "summary": summary,
                "input": arguments,
                "subagent": depth > 0,
            },
        )

        if bucket == "write":
            trace.tool_awaiting(name)
        verdict = await self.gate(name, arguments)
        if isinstance(verdict, Deny):
            trace.tool_denied(name, verdict.message)
            await self.emit(
                "tool_result",
                {
                    "id": call["id"],
                    "content": verdict.message,
                    "truncated": False,
                    "is_error": True,
                },
            )
            return verdict.message, True
        assert isinstance(verdict, Allow)

        began = time.monotonic()
        try:
            if name == TASK_TOOL:
                output = await self._run_subagent(arguments, depth=depth, usage=usage)
            else:
                output = await self.toolbox.execute(name, arguments)
            failed = False
        except asyncio.CancelledError:
            raise
        except ToolError as exc:
            output, failed = str(exc), True
        except Exception as exc:
            output, failed = f"{type(exc).__name__}: {exc}", True
        trace.tool_end(name, time.monotonic() - began, output, failed=failed)

        shown, clipped = truncate(output)
        await self.emit(
            "tool_result",
            {"id": call["id"], "content": shown, "truncated": clipped, "is_error": failed},
        )
        return output, failed

    async def _run_subagent(
        self, arguments: dict[str, Any], *, depth: int, usage: Usage
    ) -> str:
        """Run one specialist in its own context and return only its report."""
        if depth >= 1:
            return (
                "Subagents cannot delegate further. Do the work yourself with the "
                "tools you have, or report what you need back to the orchestrator."
            )
        kind = str(arguments.get("subagent_type") or "")
        definition = SUBAGENTS.get(kind)
        if definition is None:
            return f"No such subagent: {kind}. Choose one of: {', '.join(sorted(SUBAGENTS))}."
        instruction = str(arguments.get("prompt") or "").strip()
        if not instruction:
            return "`prompt` is required: the subagent cannot see this conversation."

        model = (
            settings.subagent_model_name
            if definition.model == "inherit"
            else definition.model
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": definition.prompt},
            {"role": "user", "content": instruction},
        ]
        trace.subagent_start(kind, model, instruction)
        began = time.monotonic()
        report, rounds = await self._run_agent(
            messages=messages,
            model=model,
            tool_names=definition.tools,
            max_rounds=settings.max_subagent_tool_rounds,
            usage=usage,
            stream_text=False,
            depth=depth + 1,
        )
        trace.subagent_end(kind, time.monotonic() - began, rounds, report)
        return report or "(the subagent returned no report)"


def _summary(tool_name: str, arguments: dict[str, Any]) -> str:
    """The one-line description shown on the tool card and the approval card."""
    if tool_name == TASK_TOOL:
        label = arguments.get("description") or arguments.get("prompt", "")
        return f"delegate to {arguments.get('subagent_type', '?')}: {str(label)[:120]}"
    return summarize(tool_name, arguments)


class SessionManager:
    """All live chat sessions, keyed by the browser's session id."""

    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}

    async def get(self, session_id: str) -> ChatSession:
        session = self._sessions.get(session_id)
        if session is None:
            session = ChatSession(session_id, clusters.active)
            self._sessions[session_id] = session
        return session

    def peek(self, session_id: str) -> ChatSession | None:
        return self._sessions.get(session_id)

    async def reset_all(self) -> None:
        """Tear every session down - used when the active cluster changes, so the
        next turn rebuilds the MCP config against the new profile."""
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()

    async def shutdown(self) -> None:
        await self.reset_all()


sessions = SessionManager()
