"""Builds the server map handed to `app.mcp_client.MCPHub`.

The dict shape (`{"type": "stdio"|"http", ...}`) is the same one the Claude
Agent SDK took, so nothing below had to change when the SDK was dropped - only
the consumer did.

Note on `env`: the official MCP stdio client merges these over a small default
environment that already carries PATH, so `node` and `kubectl` stay findable
without listing the whole of os.environ here.
"""

from __future__ import annotations

import logging
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, ClusterProfile, settings

log = logging.getLogger(__name__)

GITHUB_MCP_URL ="https://api.githubcopilot.com/mcp/"

# The `actions` toolset is NOT in the server's default set; without it the
# CI/CD half of this app has no tools. See PLAN Phase 1.
GITHUB_TOOLSETS = "context,repos,pull_requests,issues,actions"


LOCAL_ENTRY = (
    PROJECT_ROOT / "node_modules" / "mcp-server-kubernetes" / "dist" / "index.js"
)


def resolve_launcher() -> tuple[str, list[str]]:
    """Return (command, args) that start mcp-server-kubernetes.

    Windows detail worth knowing: since Node 20, `child_process.spawn` refuses
    to execute a `.cmd` file directly and throws EINVAL. `npx` on Windows *is*
    `npx.cmd`, so the obvious `{"command": "npx", "args": ["-y", ...]}` config
    dies at spawn time and surfaces only as an MCP server stuck in "failed"
    with `CONNECTION_CLOSED`.

    So the default is to run the locally installed package with `node`, which
    has no shim in the path and pins the version besides. `npm install` puts it
    there; npx remains the fallback for a checkout that skipped that step.
    """
    override = settings.k8s_mcp_command.strip()
    if override:
        args = shlex.split(settings.k8s_mcp_args) if settings.k8s_mcp_args.strip() else []
        return override, args

    if LOCAL_ENTRY.exists():
        node = shutil.which("node") or "node"
        return node, [str(LOCAL_ENTRY)]

    # No local install. npx works on POSIX; on Windows it hits the EINVAL above,
    # so route it through cmd.exe, which can execute the .cmd shim.
    if sys.platform == "win32":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return comspec, ["/c", "npx", "-y", "mcp-server-kubernetes"]
    return shutil.which("npx") or "npx", ["-y", "mcp-server-kubernetes"]


def pinned_kubeconfig(profile: ClusterProfile) -> str:
    """Path to a kubeconfig holding only `profile.context`, as current-context.

    mcp-server-kubernetes applies K8S_CONTEXT to its own JS client only. Its
    kubectl-backed tools (kubectl_get, kubectl_apply, ...) add `--context` just
    when the model passes one, so otherwise they silently use whatever the
    user's kubeconfig current-context is - a different cluster than the one the
    UI shows. A minified copy leaves kubectl no other context to fall back to.

    Falls back to the full kubeconfig if kubectl cannot produce the copy.
    """
    if not profile.context:
        return profile.kubeconfig_path
    kubectl = shutil.which("kubectl") or "kubectl"
    try:
        result = subprocess.run(
            [kubectl, "config", "view", "--minify", "--raw",
             "--context", profile.context],
            env={**os.environ, "KUBECONFIG": profile.kubeconfig_path},
            capture_output=True, text=True, timeout=15, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not pin kubeconfig to %s (%s); using %s",
                    profile.context, exc, profile.kubeconfig_path)
        return profile.kubeconfig_path

    # Holds credentials: keep it out of the project tree, owner-only.
    path = Path(tempfile.gettempdir()) / f"devops-copilot-{profile.name}.kubeconfig"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(result.stdout)
    return str(path)


def k8s_server(profile: ClusterProfile) -> dict[str, Any]:
    """Config for flux159/mcp-server-kubernetes, scoped to one cluster profile."""
    if settings.k8s_mcp_transport == "http":
        headers: dict[str, str] = {}
        if settings.k8s_mcp_auth_token:
            headers["X-MCP-AUTH"] = settings.k8s_mcp_auth_token
        return {"type": "http", "url": settings.k8s_mcp_http_url, "headers": headers}

    env = {
        "KUBECONFIG_PATH": pinned_kubeconfig(profile),
        "K8S_CONTEXT": profile.context,
        "MASK_SECRETS": "true",
        # kubectl logs on a chatty pod overflows the default stdout buffer.
        "SPAWN_MAX_BUFFER": "10485760",
        **profile.env,
    }
    # Panic switches — normally unset; the per-call approval flow is the real gate.
    if settings.allow_only_non_destructive_tools:
        env["ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS"] = settings.allow_only_non_destructive_tools
    if settings.allow_only_readonly_tools:
        env["ALLOW_ONLY_READONLY_TOOLS"] = settings.allow_only_readonly_tools

    command, args = resolve_launcher()
    return {
        "type": "stdio",
        "command": command,
        "args": args,
        "env": env,
        # Pin the cwd instead of inheriting uvicorn's. `kubectl_apply` takes a
        # filename and resolves it in *this* process, so without this the server
        # and the agent's own file tools disagree about what
        # `workspace/manifests/app.yaml` means - which is exactly how the agent
        # ends up unable to find a manifest it just wrote.
        "cwd": str(PROJECT_ROOT),
    }


def github_server() -> dict[str, Any]:
    """Config for the remote GitHub MCP server."""
    return {
        "type": "http",
        "url": GITHUB_MCP_URL,
        "headers": {
            "Authorization": f"Bearer {settings.github_token}",
            "X-MCP-Toolsets": GITHUB_TOOLSETS,
        },
    }


def build_mcp_servers(profile: ClusterProfile) -> dict[str, Any]:
    """The full server map for the active cluster.

    GitHub is omitted entirely when no PAT is configured, so the agent sees no
    GitHub tools rather than tools that 401 on every call.
    """
    servers: dict[str, Any] = {"k8s": k8s_server(profile)}
    if settings.has_github:
        servers["github"] = github_server()
    return servers
