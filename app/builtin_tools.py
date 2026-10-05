"""Read / Write / Edit / Glob / Grep / Bash / TodoWrite, implemented locally.

The Claude Agent SDK shipped these; nothing else does, and the agent genuinely
needs them - `manifest-author` writes YAML to disk and validates it with
`kubectl apply --dry-run=client`. They are plain Python here, with one addition
the SDK did not have: every path is resolved and checked against
`settings.file_root` (the workspace by default), so a prompt-injected
"read C:\\Users\\...\\.env" fails loudly instead of succeeding quietly. Set
FILE_TOOLS_ROOT= empty in .env to lift the restriction.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, WORKSPACE, settings

MAX_OUTPUT = 30_000
BASH_TIMEOUT_SECONDS = 120
GREP_MAX_MATCHES = 200
GLOB_MAX_RESULTS = 200

# Patterns that match every line; see _grep.
_MATCH_EVERYTHING = {".*", ".+", "^", "$", "^.*$", "", "(?s).*", "[\s\S]*"}

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".idea"}

log = logging.getLogger("agent.tools")


class ToolError(Exception):
    """Raised for a bad call; the message goes back to the model as the result."""


def _cwd() -> Path:
    """Where Bash runs and where relative paths are anchored.

    The project root, not the workspace, so that `workspace/manifests/app.yaml`
    means the same thing here, in a Bash `kubectl apply -f ...`, and to the k8s
    MCP server. Getting these three out of step is what made the agent write to
    `workspace/workspace/manifests/` and then fail to find its own file.
    """
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return PROJECT_ROOT


def _search_base() -> Path:
    """Default directory for Glob and Grep.

    The confinement root, not `_cwd()`: relative *paths* anchor at the project
    root so they agree with kubectl, but an unscoped *search* should not walk
    the whole repo - .venv, node_modules and the project's own source are noise
    the agent pays for by the token.
    """
    return settings.file_root or PROJECT_ROOT


def _inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return True
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve(raw: str, *, must_exist: bool = False) -> Path:
    """Turn whatever the model wrote into one absolute path inside the root.

    Both spellings the model reaches for are accepted - `workspace/manifests/x`
    (repo-relative, which is how the prompts and the README name it) and
    `manifests/x` (workspace-relative, which is how it thinks about its own cwd)
    - because insisting on one and erroring on the other just burns a round trip
    for no safety gain. An absolute path is taken as given.
    """
    if not raw or not str(raw).strip():
        raise ToolError("a file path is required")

    text = str(raw).strip()
    given = Path(text)
    root = settings.file_root

    if given.is_absolute():
        candidates = [given]
    else:
        candidates = [_cwd() / given, WORKSPACE / given]

    resolved = [c.resolve(strict=False) for c in candidates]
    allowed = [c for c in resolved if _inside(c, root)]
    if not allowed:
        raise ToolError(
            f"path is outside the permitted root ({root}). File tools are "
            "confined to the workspace; put the file under workspace/ instead."
        )

    # Prefer a candidate that exists, so a read never misses a file that is
    # sitting right there under the other spelling.
    path = next((c for c in allowed if c.exists()), allowed[0])

    if must_exist and not path.exists():
        raise ToolError(_not_found_message(text, path))
    return path


def _not_found_message(requested: str, resolved: Path) -> str:
    """A miss the model can act on, rather than a dead end.

    Listing the siblings turns "no such file" into a correctable mistake; without
    it the usual next move is to rewrite the file it already wrote.
    """
    message = f"no such file: {resolved} (you asked for {requested!r})"
    parent = resolved.parent
    if parent.is_dir():
        siblings = sorted(p.name for p in parent.iterdir() if p.is_file())[:20]
        if siblings:
            return f"{message}. Files in {parent}: {', '.join(siblings)}"
        return f"{message}. {parent} exists but is empty."
    root = settings.file_root or WORKSPACE
    existing = sorted(str(p.relative_to(root)) for p in _walk(root))[:20]
    if existing:
        return f"{message}. {parent} does not exist. Under {root}: {', '.join(existing)}"
    return f"{message}. {parent} does not exist, and {root} is empty."


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


def _walk(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            yield Path(dirpath) / filename


# --- implementations --------------------------------------------------------

async def _read(args: dict[str, Any]) -> str:
    path = _resolve(args.get("file_path", ""), must_exist=True)
    if path.is_dir():
        raise ToolError(f"{path} is a directory; use Glob to list it")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(int(args.get("offset") or 0), 0)
    limit = int(args.get("limit") or 2000)
    window = lines[offset : offset + limit]
    numbered = "\n".join(
        f"{i:>6}\t{line}" for i, line in enumerate(window, start=offset + 1)
    )
    if not numbered:
        return f"{path} is empty (or the requested range is past the end)."
    trailer = ""
    if offset + limit < len(lines):
        trailer = f"\n... [{len(lines) - offset - limit} more lines]"
    return _clip(numbered) + trailer


async def _write(args: dict[str, Any]) -> str:
    path = _resolve(args.get("file_path", ""))
    content = args.get("content")
    if content is None:
        raise ToolError("`content` is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(str(content), encoding="utf-8", newline="\n")
    verb = "Overwrote" if existed else "Wrote"
    log.info("%s %s (%d chars)", verb.lower(), path, len(str(content)))
    return f"{verb} {path} ({len(str(content))} characters)."


async def _edit(args: dict[str, Any]) -> str:
    path = _resolve(args.get("file_path", ""), must_exist=True)
    old = args.get("old_string")
    new = args.get("new_string")
    if old is None or new is None:
        raise ToolError("`old_string` and `new_string` are both required")
    text = path.read_text(encoding="utf-8")
    occurrences = text.count(str(old))
    if occurrences == 0:
        raise ToolError(
            "`old_string` was not found in the file. Read the file again and "
            "copy the exact text, including indentation."
        )
    if occurrences > 1 and not args.get("replace_all"):
        raise ToolError(
            f"`old_string` appears {occurrences} times. Include more surrounding "
            "context to make it unique, or pass replace_all: true."
        )
    count = -1 if args.get("replace_all") else 1
    path.write_text(text.replace(str(old), str(new), count), encoding="utf-8", newline="\n")
    replaced = occurrences if args.get("replace_all") else 1
    return f"Edited {path} ({replaced} replacement{'s' if replaced != 1 else ''})."


async def _glob(args: dict[str, Any]) -> str:
    pattern = str(args.get("pattern") or "**/*")
    base = _resolve(args["path"]) if args.get("path") else _search_base()
    if not base.exists():
        raise ToolError(f"no such directory: {base}")
    matches = sorted(
        (p for p in base.glob(pattern) if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:GLOB_MAX_RESULTS]
    if not matches:
        return f"No files match {pattern} under {base}."
    return "\n".join(str(p) for p in matches)


async def _grep(args: dict[str, Any]) -> str:
    pattern = args.get("pattern")
    if not pattern:
        raise ToolError("`pattern` is required")
    if str(pattern).strip() in _MATCH_EVERYTHING:
        # A match-everything pattern is the model reaching for a directory
        # listing with the wrong tool. Left alone it returns every line of every
        # file - one observed call pulled 23k characters of YAML into the
        # context to answer "which manifests exist".
        raise ToolError(
            f"`{pattern}` matches every line, which dumps whole files into the "
            "conversation. Use Glob to list files (e.g. pattern '**/*.yaml'), "
            "Read to see one file, or Grep with a term you are actually "
            "looking for."
        )
    flags = re.IGNORECASE if args.get("case_insensitive") else 0
    try:
        regex = re.compile(str(pattern), flags)
    except re.error as exc:
        raise ToolError(f"bad regular expression: {exc}") from None

    base = _resolve(args["path"]) if args.get("path") else _search_base()
    glob = args.get("glob")
    hits: list[str] = []
    for candidate in _walk(base):
        if glob and not fnmatch.fnmatch(candidate.name, str(glob)):
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable; not what Grep is for
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{candidate}:{number}:{line.strip()[:300]}")
                if len(hits) >= GREP_MAX_MATCHES:
                    hits.append(f"... [stopped at {GREP_MAX_MATCHES} matches]")
                    return "\n".join(hits)
    return "\n".join(hits) if hits else f"No matches for {pattern} under {base}."


async def _bash(args: dict[str, Any]) -> str:
    command = args.get("command")
    if not command or not str(command).strip():
        raise ToolError("`command` is required")
    timeout = min(int(args.get("timeout") or BASH_TIMEOUT_SECONDS), 600)

    log.debug("bash [cwd=%s] %s", _cwd(), command)
    process = await asyncio.create_subprocess_shell(
        str(command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_cwd()),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ToolError(f"command timed out after {timeout}s") from None

    out = stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip()
    code = process.returncode or 0
    if code != 0:
        # The agent will summarise this; the raw stderr is what a human debugging
        # a failed apply actually needs to see.
        log.warning("bash exited %d: %s", code, (err or out)[:600])
    body = "\n".join(part for part in (out, err and f"[stderr]\n{err}") if part)
    if code != 0:
        body = f"[exit {code}]\n{body}".strip()
    return _clip(body) or f"[exit {code}] (no output)"


async def _todo_write(args: dict[str, Any]) -> str:
    todos = args.get("todos") or []
    if not isinstance(todos, list):
        raise ToolError("`todos` must be a list")
    lines = []
    for item in todos:
        if isinstance(item, dict):
            mark = {"completed": "x", "in_progress": ">"}.get(
                str(item.get("status", "")), " "
            )
            lines.append(f"[{mark}] {item.get('content', '')}")
        else:
            lines.append(f"[ ] {item}")
    return "Plan recorded:\n" + "\n".join(lines) if lines else "Plan cleared."


# --- schemas ----------------------------------------------------------------

def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

SCHEMAS: dict[str, dict[str, Any]] = {
    "Read": _tool(
        "Read",
        "Read a text file from the workspace. Returns numbered lines.",
        {
            "file_path": {**_STR, "description": "Path to the file, e.g. workspace/manifests/app.yaml."},
            "offset": {**_INT, "description": "First line to read (0-based)."},
            "limit": {**_INT, "description": "How many lines to read. Default 2000."},
        },
        ["file_path"],
    ),
    "Write": _tool(
        "Write",
        "Write a file, creating parent directories and overwriting any existing content.",
        {
            "file_path": {**_STR, "description": "Path to write, e.g. workspace/manifests/app.yaml."},
            "content": {**_STR, "description": "Full file contents."},
        },
        ["file_path", "content"],
    ),
    "Edit": _tool(
        "Edit",
        "Replace an exact string in an existing file. Prefer this over rewriting a whole file.",
        {
            "file_path": _STR,
            "old_string": {**_STR, "description": "Exact text to replace, including indentation."},
            "new_string": {**_STR, "description": "Replacement text."},
            "replace_all": {**_BOOL, "description": "Replace every occurrence instead of requiring a unique match."},
        },
        ["file_path", "old_string", "new_string"],
    ),
    "Glob": _tool(
        "Glob",
        "List files matching a glob pattern, newest first.",
        {
            "pattern": {**_STR, "description": "Glob such as '**/*.yaml'."},
            "path": {**_STR, "description": "Directory to search. Defaults to the workspace."},
        },
        ["pattern"],
    ),
    "Grep": _tool(
        "Grep",
        "Search file contents with a regular expression. Returns path:line:text.",
        {
            "pattern": {**_STR, "description": "Python regular expression."},
            "path": {**_STR, "description": "Directory to search. Defaults to the workspace."},
            "glob": {**_STR, "description": "Only search files whose name matches this glob."},
            "case_insensitive": _BOOL,
        },
        ["pattern"],
    ),
    "Bash": _tool(
        "Bash",
        (
            "Run a shell command. The working directory is the project root, so "
            "relative paths look like `workspace/manifests/app.yaml` - the same "
            "spelling the file tools and kubectl use. Use it for "
            "`kubectl apply --dry-run=client -f <file>` and similar validation. "
            "Every call requires the user's approval."
        ),
        {
            "command": {**_STR, "description": "The command line to run."},
            "timeout": {**_INT, "description": "Seconds before the command is killed. Default 120."},
        },
        ["command"],
    ),
    "TodoWrite": _tool(
        "TodoWrite",
        "Record or update your plan for a multi-step task. Purely a scratchpad; changes nothing.",
        {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": _STR,
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        ["todos"],
    ),
}

HANDLERS = {
    "Read": _read,
    "Write": _write,
    "Edit": _edit,
    "Glob": _glob,
    "Grep": _grep,
    "Bash": _bash,
    "TodoWrite": _todo_write,
}

NAMES = frozenset(HANDLERS)


async def call(name: str, arguments: dict[str, Any]) -> str:
    return await HANDLERS[name](arguments or {})
