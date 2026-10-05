"""Policy tests. Run: .venv\\Scripts\\python.exe -m tests.test_policy

No pytest dependency - this is a plain script so it runs with the app's venv
as-is. Tool names here are the real ones reported by mcp-server-kubernetes
v4.1.6, not the ones the plan guessed at.
"""

from __future__ import annotations

import sys

from app.policy import ALLOWED_TOOLS, Bucket, classify

K = "mcp__k8s__"
G = "mcp__github__"

# (label, tool, input, expected bucket)
CASES = [
    # --- reads run without a prompt -------------------------------------
    ("get pods", f"{K}kubectl_get", {"resourceType": "pods", "namespace": "demo"}, Bucket.READ),
    ("describe", f"{K}kubectl_describe", {"resourceType": "pod", "name": "api", "namespace": "demo"}, Bucket.READ),
    ("previous logs", f"{K}kubectl_logs", {"name": "api", "namespace": "demo", "previous": True}, Bucket.READ),
    ("explain", f"{K}explain_resource", {"resource": "deployment"}, Bucket.READ),
    ("ping", f"{K}ping", {}, Bucket.READ),
    ("read kube-system is fine", f"{K}kubectl_get", {"resourceType": "pods", "namespace": "kube-system"}, Bucket.READ),
    ("describe kube-system is fine", f"{K}kubectl_describe", {"resourceType": "pod", "name": "coredns-x", "namespace": "kube-system"}, Bucket.READ),
    ("actions logs are reads", f"{G}get_job_logs", {"owner": "o", "repo": "r", "job_id": 1}, Bucket.READ),
    ("list runs is a read", f"{G}actions_list", {"owner": "o", "repo": "r", "method": "list_workflow_runs"}, Bucket.READ),
    ("read a PR diff is a read", f"{G}pull_request_read", {"owner": "o", "repo": "r", "pull_number": 7, "method": "get_diff"}, Bucket.READ),
    ("read an issue is a read", f"{G}issue_read", {"owner": "o", "repo": "r", "issue_number": 3, "method": "get"}, Bucket.READ),
    ("local file read", "Read", {"file_path": "/x/y.yaml"}, Bucket.READ),

    # --- writes require a click -----------------------------------------
    ("apply", f"{K}kubectl_apply", {"filename": "m.yaml", "namespace": "demo"}, Bucket.WRITE),
    ("scale", f"{K}kubectl_scale", {"resourceType": "deployment", "name": "api", "namespace": "demo", "replicas": 3}, Bucket.WRITE),
    ("patch", f"{K}kubectl_patch", {"resourceType": "deployment", "name": "api", "namespace": "demo"}, Bucket.WRITE),
    ("rollout restart", f"{K}kubectl_rollout", {"subCommand": "restart", "resourceType": "deployment", "name": "api", "namespace": "demo"}, Bucket.WRITE),
    ("scoped delete", f"{K}kubectl_delete", {"resourceType": "pod", "name": "api", "namespace": "demo"}, Bucket.WRITE),
    ("helm install", f"{K}install_helm_chart", {"name": "redis", "chart": "bitnami/redis", "namespace": "demo"}, Bucket.WRITE),
    ("node drain", f"{K}node_management", {"operation": "drain", "nodeName": "node-1"}, Bucket.WRITE),
    ("port forward", f"{K}port_forward", {"resourceType": "svc", "resourceName": "api", "localPort": 8080, "targetPort": 80}, Bucket.WRITE),
    ("exec in pod", f"{K}exec_in_pod", {"name": "api", "namespace": "demo", "command": ["ls", "-la"]}, Bucket.WRITE),
    ("switch context", f"{K}kubectl_context", {"operation": "set", "name": "docker-desktop"}, Bucket.WRITE),
    ("dry run still asks", "Bash", {"command": "kubectl apply --dry-run=client -f m.yaml"}, Bucket.WRITE),
    ("write a manifest", "Write", {"file_path": "manifests/app.yaml", "content": "..."}, Bucket.WRITE),
    ("commit a workflow", f"{G}create_or_update_file", {"owner": "o", "repo": "r", "path": ".github/workflows/ci.yml"}, Bucket.WRITE),
    ("trigger a workflow", f"{G}actions_run_trigger", {"owner": "o", "repo": "r", "workflow_id": "ci.yml", "method": "run_workflow"}, Bucket.WRITE),
    ("merge a PR", f"{G}merge_pull_request", {"owner": "o", "repo": "r", "pull_number": 7}, Bucket.WRITE),
    ("write an issue", f"{G}issue_write", {"owner": "o", "repo": "r", "method": "create", "title": "x"}, Bucket.WRITE),
    ("review a PR", f"{G}pull_request_review_write", {"owner": "o", "repo": "r", "pull_number": 7}, Bucket.WRITE),
    ("open a PR", f"{G}create_pull_request", {"owner": "o", "repo": "r", "title": "add manifests"}, Bucket.WRITE),

    # --- the important one: unknown tools must NOT auto-run -------------
    ("unknown github tool", f"{G}list_and_delete_everything", {"owner": "o"}, Bucket.WRITE),
    ("unknown k8s tool", f"{K}kubectl_reconnect", {}, Bucket.WRITE),
    ("tool invented tomorrow", "SomeBrandNewTool", {"x": 1}, Bucket.WRITE),

    # --- hard denials ---------------------------------------------------
    ("delete kube-system ns", f"{K}kubectl_delete", {"resourceType": "namespace", "name": "kube-system"}, Bucket.FORBIDDEN),
    ("patch kube-system", f"{K}kubectl_patch", {"resourceType": "deployment", "name": "coredns", "namespace": "kube-system"}, Bucket.FORBIDDEN),
    ("apply into kube-public", f"{K}kubectl_apply", {"filename": "x.yaml", "namespace": "kube-public"}, Bucket.FORBIDDEN),
    ("generic ns delete", f"{K}kubectl_generic", {"command": "delete", "subCommand": "namespace", "args": ["demo"]}, Bucket.FORBIDDEN),
    ("generic touching kube-system", f"{K}kubectl_generic", {"command": "edit", "args": ["deploy", "-n", "kube-system"]}, Bucket.FORBIDDEN),
    ("delete across all namespaces", f"{K}kubectl_delete", {"resourceType": "pods", "allNamespaces": True}, Bucket.FORBIDDEN),
    ("cleanup all namespaces", f"{K}cleanup", {"allNamespaces": True}, Bucket.FORBIDDEN),
    ("kubectl delete via Bash", "Bash", {"command": "kubectl delete pod api -n demo"}, Bucket.FORBIDDEN),
    ("rm -rf via Bash", "Bash", {"command": "rm -rf /important"}, Bucket.FORBIDDEN),
    ("rm -fr via Bash", "Bash", {"command": "rm -fr ~/work"}, Bucket.FORBIDDEN),
    ("helm uninstall via Bash", "Bash", {"command": "helm uninstall redis -n demo"}, Bucket.FORBIDDEN),
    ("rm -rf inside a pod", f"{K}exec_in_pod", {"name": "api", "namespace": "demo", "command": ["rm", "-rf", "/data"]}, Bucket.FORBIDDEN),
    ("-A delete via Bash", "Bash", {"command": "kubectl get pods -A | xargs kubectl delete"}, Bucket.FORBIDDEN),
]


def main() -> int:
    failures = []
    for label, tool, payload, expected in CASES:
        actual = classify(tool, payload)
        ok = actual.bucket is expected
        if not ok:
            failures.append((label, tool, expected, actual))
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {expected.value:9} {label:34} {actual.summary[:46]}")

    print()
    # Invariant: nothing that can change cluster, repo, or disk state is ever
    # pre-approved. Matched on the tool's action verb, not a loose substring -
    # `TodoWrite` contains "write" but only touches the agent's own scratch list.
    mutating_verbs = {
        "apply", "delete", "create", "patch", "scale", "rollout", "exec",
        "cleanup", "drain", "cordon", "install", "upgrade", "uninstall",
        "run", "rerun", "cancel", "push", "merge", "fork", "dispatch",
        "add", "remove", "set", "edit", "write",
    }
    exempt = {"TodoWrite"}  # agent-internal scratch list, no external effect

    def leading_verb(tool: str) -> str:
        # MCP tool names are verb-first: `run_workflow` is a trigger,
        # `get_workflow_run` is a read - "run" is a noun in the second.
        return tool.split("__")[-1].lower().split("_")[0]

    leaked = [
        tool
        for tool in ALLOWED_TOOLS
        if tool not in exempt and leading_verb(tool) in mutating_verbs
    ]
    if leaked:
        failures.append(("allowed_tools contains a mutating tool", leaked, None, None))
        print(f"FAIL allowed_tools leaked: {leaked}")
    else:
        print(f"ok   allowed_tools: {len(ALLOWED_TOOLS)} entries, none mutating")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for failure in failures:
            print("  ", failure)
        return 1
    print(f"all {len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
