"""Readable tracing for a turn, so a failure can be diagnosed from the console.

The agent's work is a long chain - model call, tool call, policy decision,
approval, MCP round trip - and when a pod will not come up the question is
always "what exactly did it ask for, and what came back". Every stage logs
through here in one consistent shape:

    17:42:10 INFO  agent       == turn   [a1b2c3d4]  deploy postgres and create a table
    17:42:10 INFO  agent       >> model  gpt-4.1-mini  msgs=2 tools=77
    17:42:12 INFO  agent       << model  gpt-4.1-mini  2.1s  in=4231 out=88 tool_calls=1
    17:42:12 INFO  agent       >> tool   [WRITE] kubectl apply -f workspace/manifests/pg.yaml
    17:42:12 INFO  agent          |      waiting for approval...
    17:42:19 INFO  agent       << tool   ok   mcp__k8s__kubectl_apply  0.4s  86 chars
    17:42:19 INFO  agent          |      deployment.apps/postgres created

Set LOG_LEVEL=DEBUG (or LOG_TOOL_IO=true) in .env to include the full arguments
and full results rather than a clipped first line.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings

log = logging.getLogger("agent")

_SECRET_HINTS = ("password", "token", "secret", "apikey", "api_key", "authorization")


def configure_logging() -> None:
    """Install the console format. Called once from main."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level_name, logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)-11s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    # These two are chatty and say nothing useful about the agent's reasoning.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)


def _redact(value: Any) -> Any:
    """Blank anything that looks like a credential before it reaches the log."""
    if isinstance(value, dict):
        return {
            k: ("***" if any(h in str(k).lower() for h in _SECRET_HINTS) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def payload(value: Any, *, limit: int | None = None) -> str:
    """One-line, redacted, clipped rendering of arguments or a result."""
    limit = limit or max(settings.log_payload_chars, 80)
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(_redact(value), default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        text = f"{text[:limit]}... (+{len(text) - limit} chars)"
    # The console here is cp1252; a stray box-drawing character from kubectl or
    # a manifest would raise UnicodeEncodeError inside logging and lose the line
    # entirely - which is the one thing a debug log must not do.
    return text.encode("ascii", "replace").decode("ascii")


def verbose() -> bool:
    """True when full tool input/output should be logged."""
    return settings.log_tool_io or settings.log_level_name == "DEBUG"


# --- the stages -------------------------------------------------------------
#
# ASCII only, deliberately: the Windows console codepage turns box-drawing and
# arrow characters into escape noise, which is exactly when you least want the
# log to be hard to read.

def model_start(model: str, messages: int, tools: int, *, depth: int = 0) -> None:
    label = "sub  " if depth else "model"
    log.info(">> %s  %s  msgs=%d tools=%d", label, model, messages, tools)


def model_end(model: str, seconds: float, usage: Any, calls: int, *, depth: int = 0) -> None:
    label = "sub  " if depth else "model"
    log.info(
        "<< %s  %s  %.1fs  in=%s out=%s tool_calls=%d",
        label,
        model,
        seconds,
        getattr(usage, "prompt_tokens", "?"),
        getattr(usage, "completion_tokens", "?"),
        calls,
    )


def tool_start(tool: str, summary: str, bucket: str, arguments: dict, *, subagent: bool) -> None:
    tag = " (sub)" if subagent else ""
    log.info(">> tool   [%s]%s %s", bucket.upper(), tag, summary)
    if verbose():
        log.info("   |      args %s", payload(arguments))
    else:
        log.debug("   |      args %s", payload(arguments))


def tool_awaiting(tool: str) -> None:
    log.info("   |      waiting for approval...")


def tool_end(tool: str, seconds: float, result: str, *, failed: bool) -> None:
    log.info(
        "<< tool   %s  %s  %.1fs  %d chars",
        "ERR" if failed else "ok ",
        tool,
        seconds,
        len(result),
    )
    if failed:
        # A failure is the whole reason someone is reading these logs; never
        # hide it behind a log level.
        log.warning("   |      %s", payload(result, limit=1200))
    elif verbose():
        log.info("   |      %s", payload(result, limit=1200))
    elif result.strip():
        log.info("   |      %s", payload(result, limit=160))


def tool_denied(tool: str, reason: str) -> None:
    log.warning("!! tool   DENIED  %s  %s", tool, payload(reason, limit=200))


def subagent_start(kind: str, model: str, instruction: str) -> None:
    log.info(">> task   %s (%s)  %s", kind, model, payload(instruction, limit=200))


def subagent_end(kind: str, seconds: float, rounds: int, report: str) -> None:
    log.info("<< task   %s  %.1fs  %d rounds  %d chars", kind, seconds, rounds, len(report))


def turn_start(session_id: str, prompt: str) -> None:
    log.info("=" * 74)
    log.info("== turn   [%s]  %s", session_id[:8], payload(prompt, limit=300))


def turn_end(seconds: float, rounds: int, usage: Any, cost: float | None) -> None:
    money = f"  ${cost:.4f}" if isinstance(cost, float) else ""
    log.info(
        "== done   %.1fs  %d rounds  in=%d out=%d cached=%d%s",
        seconds,
        rounds,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.cached_tokens,
        money,
    )
    log.info("=" * 74)
