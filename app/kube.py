"""Direct kubectl calls for the live cluster panel.

Deliberately does not go through the agent: polling the cluster every ten
seconds through an LLM would be slow and expensive. This is plain kubectl.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime, timezone
from typing import Any

from app.config import ClusterProfile

KUBECTL_TIMEOUT = 15


def _kubectl_path() -> str:
    return shutil.which("kubectl") or "kubectl"


async def _run(profile: ClusterProfile, *args: str) -> tuple[int, str, str]:
    env = {**os.environ, "KUBECONFIG": profile.kubeconfig_path, **profile.env}
    argv = [_kubectl_path()]
    if profile.context:
        argv += ["--context", profile.context]
    argv += list(args)

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=KUBECTL_TIMEOUT
        )
    except asyncio.TimeoutError:
        process.kill()
        return 124, "", f"kubectl timed out after {KUBECTL_TIMEOUT}s"
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def _age(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    try:
        started = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _pod_row(pod: dict[str, Any]) -> dict[str, Any]:
    meta = pod.get("metadata") or {}
    status = pod.get("status") or {}
    container_statuses = status.get("containerStatuses") or []

    ready = sum(1 for c in container_statuses if c.get("ready"))
    restarts = sum(int(c.get("restartCount") or 0) for c in container_statuses)

    phase = status.get("phase", "Unknown")
    # The phase says "Running" long after a container has started crash-looping;
    # the container's waiting reason is what the user actually needs to see.
    for container in container_statuses:
        waiting = (container.get("state") or {}).get("waiting") or {}
        terminated = (container.get("state") or {}).get("terminated") or {}
        if waiting.get("reason"):
            phase = waiting["reason"]
            break
        if terminated.get("reason") and terminated.get("reason") != "Completed":
            phase = terminated["reason"]
            break

    healthy = phase in ("Running", "Succeeded", "Completed") and (
        not container_statuses or ready == len(container_statuses)
    )
    return {
        "namespace": meta.get("namespace", ""),
        "name": meta.get("name", ""),
        "ready": f"{ready}/{len(container_statuses)}" if container_statuses else "0/0",
        "status": phase,
        "restarts": restarts,
        "age": _age(meta.get("creationTimestamp")),
        "healthy": healthy,
    }


async def overview(profile: ClusterProfile) -> dict[str, Any]:
    """Pods and recent warning events across the cluster."""
    pods_task = _run(profile, "get", "pods", "--all-namespaces", "-o", "json")
    events_task = _run(
        profile,
        "get",
        "events",
        "--all-namespaces",
        "--field-selector",
        "type=Warning",
        "-o",
        "json",
    )
    nodes_task = _run(profile, "get", "nodes", "-o", "json")
    (pods_rc, pods_out, pods_err), (_, events_out, _), (_, nodes_out, _) = (
        await asyncio.gather(pods_task, events_task, nodes_task)
    )

    if pods_rc != 0:
        return {
            "connected": False,
            "error": (pods_err or "kubectl failed").strip().splitlines()[-1][:400],
            "context": profile.context,
            "pods": [],
            "events": [],
            "nodes": [],
        }

    pods = [_pod_row(p) for p in (json.loads(pods_out or "{}").get("items") or [])]
    pods.sort(key=lambda p: (p["healthy"], p["namespace"], p["name"]))

    events = []
    for item in json.loads(events_out or "{}").get("items") or []:
        obj = item.get("involvedObject") or {}
        events.append(
            {
                "namespace": item.get("metadata", {}).get("namespace", ""),
                "object": f"{obj.get('kind', '')}/{obj.get('name', '')}",
                "reason": item.get("reason", ""),
                "message": (item.get("message") or "")[:300],
                "count": item.get("count", 1),
                "age": _age(
                    item.get("lastTimestamp")
                    or item.get("eventTime")
                    or item.get("metadata", {}).get("creationTimestamp")
                ),
                "_sort": item.get("lastTimestamp") or "",
            }
        )
    events.sort(key=lambda e: e["_sort"], reverse=True)
    for event in events:
        event.pop("_sort", None)

    nodes = []
    for item in json.loads(nodes_out or "{}").get("items") or []:
        conditions = {
            c.get("type"): c.get("status")
            for c in (item.get("status", {}).get("conditions") or [])
        }
        allocatable = item.get("status", {}).get("allocatable") or {}
        nodes.append(
            {
                "name": item.get("metadata", {}).get("name", ""),
                "ready": conditions.get("Ready") == "True",
                "cpu": allocatable.get("cpu", ""),
                "memory": allocatable.get("memory", ""),
            }
        )

    return {
        "connected": True,
        "context": profile.context,
        "pods": pods,
        "events": events[:25],
        "nodes": nodes,
        "unhealthy": sum(1 for p in pods if not p["healthy"]),
    }
