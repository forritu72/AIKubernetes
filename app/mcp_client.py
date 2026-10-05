"""An MCP client built on the official open-source `mcp` package.

This is the half of the old Claude Agent SDK that actually had to be replaced:
spawning MCP servers, discovering their tools, and dispatching calls. Tool
names keep the SDK's `mcp__<server>__<tool>` shape so `app/policy.py` and its
tests apply unchanged.

Lifetime note, and the reason each server gets its own asyncio task: the stdio
transport is built on anyio task groups, whose cancel scopes must be entered
and exited by the *same* task. Our `connect()` and `aclose()` are called from
different FastAPI request handlers, so holding the `async with Client(...)`
across them directly would eventually raise "attempted to exit cancel scope in
a different task". Instead the context manager lives entirely inside one
long-running task and callers talk to it over a queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable

from mcp import Client, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from app.policy import ALLOWED_TOOLS

log = logging.getLogger(__name__)

CALL_TIMEOUT_SECONDS = 120
CONNECT_TIMEOUT_SECONDS = 60
_STOP = object()

TransportFactory = Callable[[], Any]


def _build_transport(name: str, config: dict[str, Any]) -> TransportFactory:
    """Turn one entry of the server map into something `Client` accepts.

    Called lazily inside the connection task: `streamable_http_client` returns
    an async generator that can only be entered once, so it must be created
    where it is consumed rather than stored.
    """
    kind = config.get("type", "stdio")

    if kind in ("http", "sse", "streamable_http"):
        url = config["url"]
        headers = dict(config.get("headers") or {})

        def http() -> Any:
            return streamable_http_client(
                url, http_client=create_mcp_http_client(headers=headers)
            )

        return http

    params = StdioServerParameters(
        command=config["command"],
        args=list(config.get("args") or []),
        env=dict(config.get("env") or {}),
        cwd=config.get("cwd"),
    )
    # Route the server's stderr through logging. Left to itself it writes
    # straight to the console, interleaving with our own lines mid-record -
    # `kubectl`'s "No resources found in default namespace." landing in the
    # middle of a trace line is how this was noticed.
    return lambda: stdio_client(params, errlog=_stderr_to_log(name))


def _stderr_to_log(server: str):
    """A writable file the child process can inherit, drained into the log.

    It has to be a real OS pipe rather than a file-like object: the transport
    hands this straight to `subprocess`, which calls `fileno()` on it. A duck
    -typed sink makes the server fail to start with `no attribute 'fileno'`.

    The reader is a daemon thread, so a server that dies without closing its end
    cannot keep the process alive.
    """
    read_fd, write_fd = os.pipe()

    def drain() -> None:
        try:
            with os.fdopen(read_fd, "r", encoding="utf-8", errors="replace") as pipe:
                for line in pipe:
                    if line.strip():
                        log.debug("[%s] %s", server, line.rstrip())
        except (OSError, ValueError):
            pass  # pipe closed as the server shut down; nothing to report

    threading.Thread(target=drain, name=f"mcp-stderr:{server}", daemon=True).start()
    return os.fdopen(write_fd, "w", buffering=1, encoding="utf-8", errors="replace")


def _result_text(result: types.CallToolResult) -> str:
    """Flatten an MCP tool result into the string the model will read."""
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        elif getattr(block, "type", "") == "image":
            parts.append("[image content omitted]")
        else:
            parts.append(str(block))
    if not parts and result.structured_content is not None:
        return str(result.structured_content)
    return "\n".join(parts)


class MCPServerConnection:
    """One MCP server, owned start to finish by a single asyncio task."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.status = "pending"
        self.error: str | None = None
        self.tools: list[types.Tool] = []
        self._factory = _build_transport(name, config)
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        self._ready: asyncio.Future[None] | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Spawn the connection task and wait for the handshake to settle.

        Never raises: a server that fails to start is recorded as failed and
        simply contributes no tools, which is what the UI reports.
        """
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._task = loop.create_task(self._run(), name=f"mcp:{self.name}")
        try:
            await asyncio.wait_for(
                asyncio.shield(self._ready), timeout=CONNECT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            self.status, self.error = "failed", "timed out during connect"
        except Exception as exc:
            self.status, self.error = "failed", f"{type(exc).__name__}: {exc}"
        if self.status == "failed":
            log.warning("mcp %s failed: %s", self.name, self.error)
        else:
            log.info("mcp %s: %s (%d tools)", self.name, self.status, len(self.tools))

    async def _run(self) -> None:
        assert self._ready is not None
        try:
            async with Client(self._factory()) as client:
                self.tools = list((await client.list_tools()).tools)
                self.status = "connected"
                if not self._ready.done():
                    self._ready.set_result(None)
                await self._serve(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
            if not self._ready.done():
                self._ready.set_exception(exc)

    async def _serve(self, client: Client) -> None:
        """Dispatch queued calls until asked to stop."""
        while True:
            item = await self._inbox.get()
            if item is _STOP:
                return
            tool_name, arguments, future = item
            if future.cancelled():
                continue
            try:
                result = await client.call_tool(
                    tool_name, arguments, read_timeout_seconds=CALL_TIMEOUT_SECONDS
                )
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if self.status != "connected":
            raise RuntimeError(
                f"MCP server '{self.name}' is not connected ({self.error or self.status})"
            )
        future: asyncio.Future[types.CallToolResult] = (
            asyncio.get_running_loop().create_future()
        )
        began = time.monotonic()
        await self._inbox.put((tool_name, arguments, future))
        result = await future
        text = _result_text(result)
        log.debug(
            "mcp %s/%s took %.2fs, %d chars%s",
            self.name, tool_name, time.monotonic() - began, len(text),
            " (error)" if result.is_error else "",
        )
        if result.is_error:
            raise RuntimeError(text or "tool call failed")
        return text

    async def aclose(self) -> None:
        if self._task is None or self._task.done():
            return
        await self._inbox.put(_STOP)
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self.status = "closed"


class MCPHub:
    """Every MCP server for one chat session, plus the tool name registry."""

    def __init__(self, servers: dict[str, dict[str, Any]]) -> None:
        self._configs = servers
        self._connections: dict[str, MCPServerConnection] = {}
        # qualified name -> (server, bare tool name)
        self._routes: dict[str, tuple[str, str]] = {}
        self._schemas: list[dict[str, Any]] = []

    @staticmethod
    def qualify(server: str, tool: str) -> str:
        return f"mcp__{server}__{tool}"

    async def connect(self) -> None:
        for name, config in self._configs.items():
            connection = MCPServerConnection(name, config)
            await connection.start()
            self._connections[name] = connection
            for tool in connection.tools:
                qualified = self.qualify(name, tool.name)
                self._routes[qualified] = (name, tool.name)
                self._schemas.append(_openai_schema(qualified, tool))
        self._warn_on_policy_drift()

    def _warn_on_policy_drift(self) -> None:
        """Flag auto-approved names that no connected server actually offers.

        The GitHub MCP server is remote and renames its tools from time to time
        (it once folded `get_pull_request`, `get_pull_request_diff` and six
        others into a single `pull_request_read`). The failure is silent and in
        the safe direction - an unrecognised tool falls through to WRITE and
        raises an approval card - but it turns routine reads into clicks, so it
        is worth a line in the log rather than a mystery.
        """
        connected = {c.name for c in self._connections.values() if c.status == "connected"}
        stale = sorted(
            name
            for name in ALLOWED_TOOLS
            if name.startswith("mcp__")
            and name.split("__")[1] in connected
            and name not in self._routes
        )
        if stale:
            log.warning(
                "policy allowlist names %d tool(s) no connected server offers, so "
                "they will ask for approval instead of running: %s",
                len(stale),
                ", ".join(stale),
            )

    @property
    def status(self) -> list[dict[str, Any]]:
        """Per-server status, in the shape the UI's `init` handler expects."""
        return [
            {"name": c.name, "status": c.status, "error": c.error, "tools": len(c.tools)}
            for c in self._connections.values()
        ]

    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas)

    def knows(self, qualified: str) -> bool:
        return qualified in self._routes

    async def call(self, qualified: str, arguments: dict[str, Any]) -> str:
        server, tool = self._routes[qualified]
        return await self._connections[server].call(tool, arguments)

    async def aclose(self) -> None:
        for connection in self._connections.values():
            try:
                await connection.aclose()
            except Exception as exc:
                log.warning("closing mcp %s failed: %s", connection.name, exc)
        self._connections.clear()
        self._routes.clear()
        self._schemas.clear()


def _openai_schema(qualified: str, tool: types.Tool) -> dict[str, Any]:
    """MCP tool definition -> OpenAI function-tool definition."""
    parameters = dict(tool.input_schema or {})
    parameters.pop("$schema", None)
    parameters.setdefault("type", "object")
    parameters.setdefault("properties", {})

    description = (tool.description or tool.title or qualified).strip()
    return {
        "type": "function",
        "function": {
            # OpenAI caps function names at 64 chars; no current k8s or GitHub
            # MCP tool comes close, but a truncated name would silently fail to
            # route, so clip deterministically rather than let the API reject it.
            "name": qualified[:64],
            "description": description[:1024],
            "parameters": parameters,
        },
    }
