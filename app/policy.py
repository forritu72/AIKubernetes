"""Tool-call classification: READ (auto) / WRITE (approval) / FORBIDDEN (hard deny).

Design rules, in order of importance:

1. READ is an explicit allowlist of tool names. Never a prefix glob: a future
   MCP tool named `list_and_cancel_runs` would glob its way into auto-approval.
2. Unknown tools default to WRITE. A tool this file has never heard of is
   something the user gets asked about, never something that just runs.
3. FORBIDDEN is checked first and wins over everything, including a tool that
   would otherwise be READ.
4. Bash is in WRITE *and* pattern-scanned by FORBIDDEN, because Bash is the
   escape hatch that can smuggle `kubectl delete` past a name-based policy.

On protected namespaces: reading kube-system is allowed and useful (coredns
logs explain a lot of cluster-wide failures). Mutating it is hard-denied.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

PROTECTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}


class Bucket(str, Enum):
    READ = "read"
    WRITE = "write"
    FORBIDDEN = "forbidden"


# --- READ: enumerated by name, never by pattern -----------------------------

K8S_READ_TOOLS = {
    "mcp__k8s__kubectl_get",
    "mcp__k8s__kubectl_describe",
    "mcp__k8s__kubectl_logs",
    "mcp__k8s__explain_resource",
    "mcp__k8s__list_api_resources",
    "mcp__k8s__ping",
}

GITHUB_READ_TOOLS = {
    # repo / code context
    "mcp__github__get_me",
    "mcp__github__get_file_contents",
    "mcp__github__list_branches",
    "mcp__github__list_commits",
    "mcp__github__get_commit",
    "mcp__github__list_tags",
    "mcp__github__get_tag",
    "mcp__github__search_code",
    "mcp__github__search_commits",
    "mcp__github__search_repositories",
    # issues. `issue_read` is a dispatcher: every one of its `method` values
    # (get, get_comments, get_sub_issues, get_parent, get_labels) is a read.
    # Mutation lives in the separate `issue_write`, which is not on this list.
    "mcp__github__issue_read",
    "mcp__github__list_issues",
    "mcp__github__list_issue_types",
    "mcp__github__list_issue_fields",
    "mcp__github__search_issues",
    # pull requests. Same shape: `pull_request_read` dispatches only reads
    # (get, get_diff, get_status, get_files, get_commits, get_reviews, ...);
    # `pull_request_review_write` and `merge_pull_request` are WRITE.
    "mcp__github__pull_request_read",
    "mcp__github__list_pull_requests",
    "mcp__github__search_pull_requests",
    # actions - reads only. `actions_get` and `actions_list` dispatch nothing
    # but gets and lists; triggering, re-running, cancelling, and deleting run
    # logs are all `actions_run_trigger`, which is deliberately absent here.
    "mcp__github__actions_get",
    "mcp__github__actions_list",
    "mcp__github__get_job_logs",
    # releases / labels
    "mcp__github__list_releases",
    "mcp__github__get_latest_release",
    "mcp__github__get_release_by_tag",
    "mcp__github__get_label",
}

BUILTIN_READ_TOOLS = {
    "Read",
    "Glob",
    "Grep",
    "TodoWrite",  # writes only the agent's own scratch todo list
    "Task",       # dispatches a subagent; the subagent's own calls are gated
}

READ_TOOLS: set[str] = K8S_READ_TOOLS | GITHUB_READ_TOOLS | BUILTIN_READ_TOOLS

# Every tool that runs without asking. `app/agent.py` checks each call against
# `classify()` before running it, so this list is the gate rather than a
# pre-approval hint - a name missing from it means an approval card, never a
# silent run. `app.mcp_client` logs a warning for any name here that no
# connected server actually offers, which is how upstream renames get noticed.
ALLOWED_TOOLS: list[str] = sorted(READ_TOOLS)


# --- FORBIDDEN patterns -----------------------------------------------------

_BASH_FORBIDDEN = [
    (
        re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*[rR]"),
        "recursive force delete (`rm -rf`)",
    ),
    (
        re.compile(r"\bkubectl\b[^|;&]*\bdelete\b"),
        "`kubectl delete` through Bash - use the k8s tools so the call can be reviewed",
    ),
    (
        re.compile(r"\bhelm\b[^|;&]*\b(uninstall|delete)\b"),
        "`helm uninstall` through Bash - use the helm tool so the call can be reviewed",
    ),
    (re.compile(r":\(\)\s*\{.*\};\s*:"), "fork bomb"),
    (re.compile(r"\bmkfs\b|\bdd\s+if=.*of=/dev/"), "raw disk write"),
]

_DELETE_VERB = re.compile(r"\bdelete\b|\bdestroy\b|\bpurge\b")
_ALL_NAMESPACES = re.compile(r"--all-namespaces\b|(?<![\w-])-A(?![\w-])")


@dataclass
class Decision:
    bucket: Bucket
    reason: str
    summary: str


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _as_command(value: Any) -> str:
    """Flatten a command that may arrive as a string or an argv list.

    Joining with spaces matters: JSON-encoding `["rm", "-rf", "/data"]` yields
    `"rm", "-rf", "/data"`, and the comma defeats every `\\brm\\s+-rf\\b` pattern
    below. Argv lists must be rendered the way a shell would show them.
    """
    if isinstance(value, list):
        return " ".join(_as_command(item) for item in value)
    if isinstance(value, str):
        return value
    return _as_text(value)


def _namespaces_in(tool_input: dict[str, Any]) -> set[str]:
    """Namespaces named anywhere in the call - explicit field or inside a command."""
    found: set[str] = set()
    explicit = tool_input.get("namespace")
    if isinstance(explicit, str) and explicit:
        found.add(explicit)

    blob = " ".join(
        _as_command(tool_input.get(key, ""))
        for key in ("command", "subCommand", "args", "manifest", "yaml", "resourceType", "name", "input")
    )
    for match in re.finditer(r"(?:-n|--namespace[= ])\s*([A-Za-z0-9-]+)", blob):
        found.add(match.group(1))
    for protected in PROTECTED_NAMESPACES:
        if re.search(rf"\b{re.escape(protected)}\b", blob):
            found.add(protected)
    return found


def _is_mutating(tool_name: str) -> bool:
    return tool_name not in READ_TOOLS


def _check_forbidden(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Return a denial reason, or None if the call is not hard-denied."""
    short = tool_name.split("__")[-1]

    # Bash and exec_in_pod are both arbitrary-command escape hatches - one on
    # this machine, one inside a container - so both get the same scan.
    if tool_name in ("Bash", "BashOutput", "KillShell") or short == "exec_in_pod":
        command = " ".join(
            _as_command(tool_input.get(key, ""))
            for key in ("command", "args")
        )
        for pattern, why in _BASH_FORBIDDEN:
            if pattern.search(command):
                return f"Blocked by policy: {why}."

    if _is_mutating(tool_name):
        hit = _namespaces_in(tool_input) & PROTECTED_NAMESPACES
        if hit:
            return (
                f"Blocked by policy: mutating {', '.join(sorted(hit))} is never "
                "permitted. Control-plane namespaces are read-only here."
            )

    blob = _as_text(tool_input)
    # `-A` and `--all-namespaces` only mean anything to kubectl. Inside a
    # container they are just flags: `psql -A -c "delete from users where ..."`
    # is an ordinary SQL statement, and matching it here denied it with a
    # nonsensical message about namespaces. Running `kubectl delete` through
    # exec_in_pod is still hard-denied - by the command scan above, which covers
    # exec_in_pod explicitly.
    if short != "exec_in_pod":
        if _ALL_NAMESPACES.search(blob) and _DELETE_VERB.search(blob):
            return (
                "Blocked by policy: a delete spanning all namespaces. Target one "
                "namespace explicitly."
            )

    # The server names this tool `cleanup`; the plan called it `cleanup_pods`.
    # Accept both so a rename upstream cannot quietly reopen the hole.
    if short in ("kubectl_delete", "cleanup", "cleanup_pods") and (
        tool_input.get("allNamespaces") or tool_input.get("all")
    ):
        return "Blocked by policy: a delete spanning all namespaces."

    if short == "kubectl_generic":
        command = " ".join(
            _as_command(tool_input.get(key, ""))
            for key in ("command", "subCommand", "args")
        )
        if re.search(r"\bdelete\b[^|;&]*\b(ns|namespace|namespaces)\b", command):
            return (
                "Blocked by policy: deleting a namespace wholesale. Delete the "
                "specific resources instead."
            )
    return None


# --- Human-readable summary for the approval card ---------------------------

def summarize(tool_name: str, tool_input: dict[str, Any]) -> str:
    """One compact line describing exactly what is about to run."""
    short = tool_name.split("__")[-1]
    get = tool_input.get

    def ns() -> str:
        return f" -n {get('namespace')}" if get("namespace") else ""

    if short in ("kubectl_get", "kubectl_describe", "kubectl_delete"):
        verb = short.replace("kubectl_", "")
        name = f"/{get('name')}" if get("name") else ""
        return f"kubectl {verb} {get('resourceType', '')}{name}{ns()}".strip()
    if short == "kubectl_logs":
        previous = " --previous" if get("previous") else ""
        return f"kubectl logs {get('name', '')}{ns()}{previous}".strip()
    if short == "kubectl_scale":
        target = f"{get('resourceType', 'deployment')}/{get('name', '')}"
        return f"kubectl scale {target}{ns()} --replicas={get('replicas', '?')}"
    if short == "kubectl_patch":
        return f"kubectl patch {get('resourceType', '')}/{get('name', '')}{ns()}"
    if short == "kubectl_rollout":
        target = f"{get('resourceType', '')}/{get('name', '')}"
        return f"kubectl rollout {get('subCommand', '')} {target}{ns()}"
    if short in ("kubectl_apply", "kubectl_create"):
        verb = short.replace("kubectl_", "")
        source = get("filename") or ("<inline manifest>" if get("manifest") else "")
        return f"kubectl {verb} -f {source}{ns()}".strip()
    if short == "kubectl_generic":
        args = get("args")
        rendered = " ".join(args) if isinstance(args, list) else _as_text(args or "")
        parts = [get("command", ""), get("subCommand", ""), rendered]
        return "kubectl " + " ".join(p for p in parts if p)
    if short in ("cleanup", "cleanup_pods"):
        return f"clean up problematic pods{ns() or ' (all namespaces)'}"
    if short == "exec_in_pod":
        args = get("command")
        rendered = " ".join(args) if isinstance(args, list) else _as_text(args or "")
        container = f" -c {get('container')}" if get("container") else ""
        return f"kubectl exec {get('name', '')}{ns()}{container} -- {rendered}"
    if short == "kubectl_context":
        operation = get("operation", "")
        return f"kubectl config {operation} {get('name', '') or get('context', '')}".strip()
    if short == "kubectl_reconnect":
        return "reconnect the kubernetes client"
    if short == "node_management":
        return f"node {get('operation', '')} {get('nodeName', '')}"
    if short.endswith("helm_chart"):
        verb = short.replace("_helm_chart", "")
        return f"helm {verb} {get('name', '')} ({get('chart', '')}){ns()}"
    if short in ("port_forward", "stop_port_forward"):
        target = f"{get('resourceType', '')}/{get('resourceName', '')}"
        return f"{short} {target} {get('localPort', '')}:{get('targetPort', '')}"
    if tool_name == "Bash":
        return _as_text(get("command", ""))
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        return f"{tool_name} {get('file_path', '')}"
    if tool_name.startswith("mcp__github__"):
        repo = f"{get('owner', '')}/{get('repo', '')}".strip("/")
        # Several GitHub tools are dispatchers keyed on `method`; without it the
        # card would read the same for a diff and a merge.
        method = f" {get('method')}" if get("method") else ""
        number = get("pull_number") or get("issue_number") or get("run_id") or ""
        detail = get("branch") or get("path") or get("title") or get("workflow_id") or ""
        return f"github: {short}{method} {repo} {number} {detail}".strip()
    return f"{short} {_as_text(tool_input)[:160]}"


def _known(tool_name: str) -> bool:
    known_writes = {"Write", "Edit", "Bash"}
    return (
        tool_name in known_writes
        or tool_name.startswith("mcp__k8s__")
        or tool_name.startswith("mcp__github__")
    )


def classify(tool_name: str, tool_input: dict[str, Any]) -> Decision:
    """Bucket a single tool call."""
    tool_input = tool_input or {}
    summary = summarize(tool_name, tool_input)

    denial = _check_forbidden(tool_name, tool_input)
    if denial:
        return Decision(Bucket.FORBIDDEN, denial, summary)

    if tool_name in READ_TOOLS:
        return Decision(Bucket.READ, "read-only tool", summary)

    reason = (
        "writes to the cluster, the repo, or disk"
        if _known(tool_name)
        else "unrecognised tool - unknown tools always require approval"
    )
    return Decision(Bucket.WRITE, reason, summary)
