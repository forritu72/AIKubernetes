# Local DevOps Copilot — Web UI + Agents over Kubernetes & GitHub MCP

## Context

**Short answer: yes, `flux159/mcp-server-kubernetes` is a good fit — it is the right tool layer for this, but not the product.** It is an MCP server that wraps `kubectl` and `helm` and exposes ~20 tools (`kubectl_get`, `kubectl_describe`, `kubectl_logs`, `kubectl_apply`, `kubectl_patch`, `kubectl_scale`, `kubectl_rollout`, `kubectl_delete`, `kubectl_generic`, `cleanup_pods`, `node_management`, `port_forward`, `install_helm_chart`, `explain_resource`, …). It has no UI, no chat, no agent loop, no memory, no approval flow. It is the hands; we build the brain and the face.

What we build around it:

- **Brain** — *(revised 2026-09-17: this was the Claude Agent SDK; it was replaced because the API bill was too high.)* The agent loop, context management, built-in `Read`/`Write`/`Edit`/`Bash`/`Glob`/`Grep` (needed to *author* manifests and run `kubectl --dry-run`), MCP client wiring, subagents, and the permission callback are now this project's own code in `app/`, roughly 700 lines. The model is reached through the `openai` client against any **OpenAI-compatible** endpoint (`OPENAI_BASE_URL`), so OpenAI, a local Ollama, vLLM, or a gateway are all a config line. MCP is the official open-source `mcp` package. Nothing is vendor-locked, and the cost knobs (cheaper subagent model, history budget, tool-result clipping, round caps) are ours to set.
- **Face** — FastAPI serving a single-page chat UI, streaming agent output over SSE, with inline approval cards for anything destructive.
- **GitHub** — the remote GitHub MCP server (`https://api.githubcopilot.com/mcp/`) with a PAT, for branching, committing generated manifests **and GitHub Actions workflow files**, opening PRs, and reading Actions run logs to debug red pipelines.

Intended outcome: a locally hosted chat console where you say *"the payments pods are crashlooping, find out why and fix it"*, *"write a Deployment + Service + ConfigMap for this repo and open a PR"*, or *"give this repo a CI pipeline that builds the image and validates the manifests"*, and an agent does the DevOps work against a real cluster — with you approving every write.

**Everything runs locally.** The agent, the UI, and the cluster are all on this machine; the only outbound traffic is to the model endpoint (OpenAI by default, or nothing at all if you point `OPENAI_BASE_URL` at a local Ollama) and the GitHub API. Nothing deploys *from* GitHub *into* the local cluster — see Phase 6 for why, and for the seam that makes it work on EKS later.

### Environment as verified (2026-09-10)

| Tool | Status |
|---|---|
| `claude` CLI | ➖ no longer used — the Claude Agent SDK was removed on 2026-09-17 |
| Python | ✅ 3.10.11 venv at `.venv\` (empty except pip/setuptools/wheel) |
| Node / npx | ✅ v24.20.0 |
| Docker | ✅ 28.4.0, `desktop-linux` context active |
| kubectl | ✅ v1.34.1 (Docker Desktop bundle) |
| **Kubernetes cluster** | ❌ **none** — `kubectl config get-contexts` is empty |
| helm / kind / minikube | ❌ not installed |
| `GITHUB_TOKEN` | ✅ set |
| `OPENAI_API_KEY` | ❌ not set — required before the app will start |

### Decisions locked with the user

- Local cluster: **Docker Desktop Kubernetes** (enable in Settings → Kubernetes; creates the `docker-desktop` context)
- UI: **FastAPI + single-page HTML/JS**, SSE streaming, no npm build step
- Safety: **reads auto-approved, every write requires an in-UI approval click**
- GitHub: **remote GitHub MCP over HTTP with a fine-grained PAT**
- CI/CD: **agent authors GitHub Actions workflows**; pipelines build/push/validate on GitHub-hosted runners, the local agent does the `apply`. EKS deploy job written but disabled (Phase 6)
- Actions scope: **reading runs and debugging failures is auto-approved; triggering and re-running is approval-gated; no secrets management** (keeps the PAT narrow)

---

## Full capability list

What the finished system can do, grouped by area. **R** = read-only, runs without asking. **A** = requires an approval click in the UI. **✗** = hard-denied, never offered.

### Cluster observation (R)
- List and inspect any resource type — pods, deployments, statefulsets, daemonsets, jobs, cronjobs, services, ingresses, configmaps, secrets, PVCs, nodes, namespaces, CRDs
- Describe resources with full event history
- Stream and read pod logs, including `--previous` logs from a crashed container
- Read cluster events, filtered by namespace and severity
- Explain any resource type's schema (`explain_resource`) and list available API resources
- Live cluster overview panel — pod status table and recent warning events, refreshed on a timer without spending tokens
- Check node capacity, allocatable resources, taints, and conditions

### Diagnosis (R)
- Root-cause `CrashLoopBackOff` — reads previous-container logs, exit codes, and restart counts
- Root-cause `ImagePullBackOff` / `ErrImagePull` — bad tag, missing registry credentials, wrong registry host
- Root-cause `Pending` pods — unschedulable due to resource requests, node selectors, taints, or missing PVCs
- Root-cause `OOMKilled` — compares memory limits against actual usage patterns
- Diagnose failing readiness/liveness probes and service→pod selector mismatches
- Trace why a Service returns nothing — endpoint/selector/port mismatches
- Explain a stuck rollout (`kubectl rollout status` never converging)
- Delegates to the `k8s-diagnostician` subagent so log-heavy investigation doesn't flood the main conversation

### Cluster mutation (A — every one of these raises an approval card)
- Apply manifests, create resources, patch fields
- Scale deployments and statefulsets
- Restart deployments and manage rollouts (pause, resume, undo/rollback, status)
- Delete resources
- Clean up problematic pods in bulk (`cleanup_pods` — Evicted, CrashLoopBackOff, Completed)
- Cordon, drain, and uncordon nodes for maintenance
- Port-forward to a pod or service, and stop the forward
- Install, upgrade, and uninstall Helm charts with custom values and repos
- Run arbitrary kubectl via `kubectl_generic` when no dedicated tool fits
- Switch kubectl contexts

### Manifest and config authoring (A on write)
- Write Deployments, StatefulSets, DaemonSets, Jobs, CronJobs
- Write Services (ClusterIP/NodePort/LoadBalancer), Ingresses, NetworkPolicies
- Write ConfigMaps and Secrets, HPAs, PodDisruptionBudgets, ResourceQuotas, LimitRanges
- Write ServiceAccounts, Roles, RoleBindings — RBAC scaffolding
- Add production-shaped defaults: resource requests/limits, liveness/readiness/startup probes, security contexts, anti-affinity, rolling-update strategy
- Validate before applying — `kubectl apply --dry-run=client` and `kubeconform` on every generated file
- Generate Helm chart scaffolding and values files
- Generate Kustomize bases and per-environment overlays
- Read an existing app repo and infer what its manifests should look like (ports, env vars, build output, Dockerfile)
- Write Dockerfiles and `.dockerignore` for a repo that lacks them

### GitHub (R for reads, A for writes)
- Browse repos, read files, search code, read issues and PRs (R)
- Create branches (A)
- Commit generated manifests and workflow files (A)
- Open pull requests with a written description of what changed and why (A)
- Read PR review comments and revise (R read / A commit)

### CI/CD pipelines
- Author `.github/workflows/*.yml` — build → push image to GHCR → `kubeconform` → `kubectl --dry-run` (A on commit)
- Author PR-validation workflows that gate manifest changes (A on commit)
- Author an EKS deploy job with OIDC role assumption, present but disabled until `DEPLOY_TARGET` is set (A on commit)
- Watch workflow runs — list runs, read job logs, see which step failed (R)
- Debug a red pipeline and explain the failure, then propose a fix (R to diagnose, A to commit)
- Trigger a workflow via `workflow_dispatch`, re-run failed jobs, cancel a run (A)
- Close the deploy loop locally — take the image tag a green pipeline produced, update the Deployment manifest, apply it to the local cluster (A)

### Safety and control
- Every destructive action pauses and shows the exact command before it runs
- Approve or deny each one individually; denial is reported back to the agent, which does not blindly retry
- Hard-denied regardless of approval (✗): anything touching `kube-system` / `kube-public`, `--all-namespaces` deletes, `rm -rf`, `kubectl delete` smuggled through Bash
- Unknown/new tools default to requiring approval, never to auto-run
- Secrets masked in `kubectl get secrets` output
- Agent holds no GitHub Secrets permission — it cannot create credentials for the workflows it writes
- Stop button interrupts a running turn mid-tool-call
- Bound to `127.0.0.1` only

### Session and multi-cluster
- Multi-turn chat with full context across a session — follow-ups like "now scale that to 3" work
- Token-by-token streaming, with collapsible cards for each tool call and its output
- Optional thinking panel showing the agent's reasoning
- Per-turn cost and turn count reporting
- Named cluster profiles in `clusters.yaml`; switch clusters from the UI
- Same UI drives an EKS cluster later — add a profile after `aws eks update-kubeconfig`, no code change
- Specialist subagents (`k8s-diagnostician`, `manifest-author`, `pipeline-author`, `repo-deployer`, `pipeline-doctor`) keep long investigations out of the main context

### Not included (deliberately)
- No end-to-end CD from GitHub into the local cluster — GitHub-hosted runners cannot reach this machine (see Phase 5). Real pipeline-driven deploys arrive with EKS.
- No secrets management, no multi-user auth, no audit database, no cost/FinOps analysis, no service-mesh or observability-stack management. All are addable later on the same foundation.

---

## Target layout

```
Kubernates_Manager/
├─ requirements.txt
├─ .env.example              # OPENAI_*, GITHUB_TOKEN, KUBECONFIG, APP_PORT
├─ clusters.yaml             # cluster profiles — the AWS seam
├─ run.ps1                   # venv activate + uvicorn
├─ app/
│  ├─ main.py                # FastAPI: static mount, SSE chat, approvals, cluster REST
│  ├─ config.py              # pydantic-settings + clusters.yaml loader
│  ├─ mcp_config.py          # builds the mcp_servers dict per active cluster profile
│  ├─ policy.py              # tool-call classification: READ / WRITE / FORBIDDEN
│  ├─ approvals.py           # pending-approval registry (asyncio.Future per request)
│  ├─ permissions.py         # permission gate → policy + approvals
│  ├─ prompts.py             # orchestrator system prompt + subagent definitions
│  ├─ mcp_client.py          # MCP client: spawn, discover, dispatch  (added 2026-09-17)
│  ├─ builtin_tools.py       # Read/Write/Edit/Glob/Grep/Bash        (added 2026-09-17)
│  ├─ agent.py               # the agent loop + session lifecycle
│  ├─ events.py              # SSE framing + token/cost accounting
│  └─ kube.py                # direct kubectl calls for the live side panel (no LLM)
├─ static/{index.html, app.js, styles.css}
└─ workspace/                # agent cwd — generated manifests, notes
   └─ manifests/
```

---

## Implementation

### Phase 0 — Cluster + prerequisites (do first, it gates everything)

1. Docker Desktop → Settings → Kubernetes → **Enable Kubernetes** → Apply & Restart. Verify: `kubectl config current-context` → `docker-desktop`, `kubectl get nodes` → one `Ready` node.
2. Install Helm (`winget install Helm.Helm`) — `mcp-server-kubernetes`'s Helm tools are dead weight without it.
3. Create a fine-grained GitHub PAT scoped to the target repos. Required permissions:

   | Permission | Level | Why |
   |---|---|---|
   | Contents | Read/write | Commit manifests and workflow files |
   | Pull requests | Read/write | Open PRs |
   | **Workflows** | **Read/write** | **Required to commit anything under `.github/workflows/` — GitHub rejects the push without it, with a confusing 403. This is the single most common thing to get wrong.** |
   | Actions | Read/write | Read run logs (read) + `workflow_dispatch` and re-run (write, approval-gated) |
   | Issues | Read | Context when investigating |

   Secrets stays **unset** — the agent does not manage secrets. Put the token in `.env` as `GITHUB_TOKEN`.
4. `pip install -r requirements.txt` — `openai`, `mcp`, `fastapi`, `uvicorn[standard]`, `pydantic-settings`, `python-dotenv`, `pyyaml`. *(Was `claude-agent-sdk` until 2026-09-17.)* SSE is hand-rolled with `StreamingResponse`; no `sse-starlette` needed.
5. Smoke-test the MCP server standalone before wiring it in: `npx -y mcp-server-kubernetes` should start and log a stdio handshake.

### Phase 1 — MCP wiring (`app/mcp_config.py`, `app/config.py`)

`build_mcp_servers(profile)` returns the dict passed to `MCPHub` (originally `ClaudeAgentOptions.mcp_servers`; the shape is unchanged):

```python
{
  "k8s": {
      "command": "npx",
      "args": ["-y", "mcp-server-kubernetes"],
      "env": {
          "KUBECONFIG_PATH": profile.kubeconfig,
          "K8S_CONTEXT": profile.context,
          "MASK_SECRETS": "true",
          "SPAWN_MAX_BUFFER": "10485760",
          **profile.env,          # AWS_PROFILE / AWS_REGION for EKS later
      },
  },
  "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {
          "Authorization": f"Bearer {settings.github_token}",
          "X-MCP-Toolsets": "context,repos,pull_requests,issues,actions",
      },
  },
}
```

The `actions` toolset is what makes CI/CD work — the default toolset set does **not** include it. Verify the toolset-selection mechanism when wiring this up: the remote server supports both a header and per-toolset URL paths (`https://api.githubcopilot.com/mcp/x/actions`). If the header is ignored, read the tool list from the `system`/`init` message and switch to the path form. Confirm `list_workflow_runs` / `get_job_logs` appear before building on them.

Deliberately **not** setting `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS` / `ALLOW_ONLY_READONLY_TOOLS` — that would blunt the server permanently. Destructive tools stay available and are gated per-call by our own approval flow (Phase 2), which is what the user asked for. Those env vars are documented in `.env.example` as a panic switch.

`clusters.yaml` is the **AWS seam**:

```yaml
default: local
profiles:
  local:
    context: docker-desktop
    kubeconfig: ~/.kube/config
  # eks-prod:                      # future — no code change needed
  #   context: arn:aws:eks:us-east-1:123456789012:cluster/prod
  #   kubeconfig: ~/.kube/config
  #   env: {AWS_PROFILE: prod, AWS_REGION: us-east-1}
```

Going to EKS later = `aws eks update-kubeconfig --name prod` + uncomment a profile. `mcp-server-kubernetes` shells out to the local `kubectl`, so IAM/exec-credential auth is handled by the AWS CLI, not by us. Switching profiles in the UI tears down the SDK client and starts a fresh one with new MCP env.

**Windows gotcha to verify here:** stdio MCP servers spawn `npx`, which is `npx.cmd` on Windows. If the server reports `failed` in the init message, fall back in this order: (a) `npm i -g mcp-server-kubernetes` and use the installed `.cmd` shim as `command`; (b) run it in streamable-HTTP mode as a managed subprocess — `ENABLE_UNSAFE_STREAMABLE_HTTP_TRANSPORT=1`, `PORT=3001`, `MCP_AUTH_TOKEN=<random>` — and connect with `{"type": "http", "url": "http://localhost:3001/mcp", "headers": {"X-MCP-AUTH": token}}`. Write the config builder so either path is a one-line switch.

### Phase 2 — Policy + approvals (`policy.py`, `approvals.py`, `permissions.py`)

Three buckets, decided by tool name plus a look at the arguments:

| Bucket | Tools | Behavior |
|---|---|---|
| **READ** (auto) | `mcp__k8s__kubectl_get`, `kubectl_describe`, `kubectl_logs`, `explain_resource`, `list_api_resources`, `ping`; `mcp__github__get_*`/`list_*`/`search_*` — which covers the Actions read tools `list_workflow_runs`, `get_workflow_run`, `get_job_logs`, `list_workflow_jobs`; built-in `Read`, `Glob`, `Grep` | Run immediately, no prompt |
| **WRITE** (approval) | `kubectl_apply`, `kubectl_create`, `kubectl_patch`, `kubectl_scale`, `kubectl_rollout`, `kubectl_delete`, `kubectl_generic`, `cleanup_pods`, `node_management`, `port_forward`, all `*_helm_chart`; `mcp__github__create_branch`/`create_or_update_file`/`create_pull_request`/`push_*`, plus the Actions write tools `run_workflow`, `rerun_workflow_run`, `rerun_failed_jobs`, `cancel_workflow_run`; built-in `Write`, `Edit`, `Bash` | Emit an approval card, block on an `asyncio.Future`, resolve on the user's click |
| **FORBIDDEN** (hard deny) | anything touching `kube-system`/`kube-public`; `--all-namespaces` combined with a delete verb; `kubectl_generic` containing `delete ns`; `Bash` matching `rm -rf` or `kubectl delete` | `PermissionResultDeny`, never reaches the user |

`can_use_tool(tool_name, input_data, context)` classifies, and for WRITE registers `{id, tool, input, summary}` in the approvals registry, pushes an `approval_request` SSE event, and awaits the future (10-minute timeout → deny). `POST /api/approve {request_id, decision}` resolves it. `PermissionResultAllow(updated_input=...)` is how an edited command gets approved, so the card can be made editable later.

Two things to get right:
- `allowed_tools` **pre-approves**, it does not gate — put only the READ list there. Do **not** use `permission_mode="bypassPermissions"`; it auto-approves MCP tools and defeats the whole design. Use `permission_mode="default"` and let `can_use_tool` decide.
- `Bash` is the escape hatch that makes the policy leaky (`kubectl delete` via Bash). It is in the WRITE bucket *and* pattern-checked in FORBIDDEN — both, not either.

### Phase 3 — Agent layer (`prompts.py`, `agent.py`, `events.py`, `mcp_client.py`, `builtin_tools.py`)

> **Revised 2026-09-17.** Phase 3 originally delegated the loop to the Claude
> Agent SDK. It is now implemented directly:
>
> - `mcp_client.py` — MCP over the official `mcp` package. Each server is owned
>   by one asyncio task, because the stdio transport's anyio cancel scopes must
>   be entered and exited by the same task and our connect/close happen in
>   different request handlers.
> - `builtin_tools.py` — the file and shell tools the SDK used to supply, now
>   confined to `workspace/` by path resolution.
> - `agent.py` — the loop (stream → accumulate tool-call deltas → gate → execute
>   → feed back), subagent dispatch via a `Task` tool, and history trimming that
>   cuts only at user boundaries so a `tool` message is never orphaned.
> - `events.py` — SSE framing plus token/cost accounting.
>
> The SSE event names and `app/policy.py` were deliberately left unchanged, so
> the UI and the whole safety model carried over untouched.

One long-lived `ClaudeSDKClient` per browser chat session, kept in a session dict keyed by `session_id`:

```python
options = ClaudeAgentOptions(
    model="claude-opus-5",
    system_prompt={"type": "preset", "preset": "claude_code", "append": DEVOPS_PROMPT},
    cwd=str(WORKSPACE),
    mcp_servers=build_mcp_servers(profile),
    allowed_tools=READ_TOOLS,          # wildcards: "mcp__k8s__kubectl_get", ...
    can_use_tool=permission_handler,
    agents=SUBAGENTS,
    permission_mode="default",
    setting_sources=[],                 # ignore ~/.claude and repo settings — this app owns its config
    include_partial_messages=True,      # token-level streaming to the browser
    env={"MCP_TIMEOUT": "60000"},
)
```

Verify the `system_prompt` preset shape against the installed SDK's `types.py` when writing this; if it differs, a plain string prompt works and only loses Claude Code's built-in tool guidance.

`DEVOPS_PROMPT` encodes the working style: diagnose before mutating; always `kubectl_get`/`describe`/`logs` first; state the root cause and the exact fix before requesting a write; prefer declarative manifests in `workspace/manifests/` over imperative one-offs; always `kubectl apply --dry-run=client` a manifest before the real apply; never touch `kube-system`; name the namespace in every command.

Subagents via `agents=` (each an `AgentDefinition`), so investigation doesn't flood the main context:

| Subagent | Tools | Job |
|---|---|---|
| `k8s-diagnostician` | READ tools only | CrashLoopBackOff / ImagePullBackOff / Pending / OOMKilled triage — pods, events, logs, previous-container logs, resource requests. Returns root cause + proposed fix, mutates nothing |
| `manifest-author` | `Read`, `Write`, `Edit`, `Bash`, `explain_resource` | Writes Deployment/Service/Ingress/ConfigMap/Secret/HPA YAML into `workspace/manifests/`, validates with `kubectl apply --dry-run=client -f` |
| `pipeline-author` | `Read`, `Write`, `Edit`, `Bash`, `mcp__github__get_*`/`list_*` | Inspects a repo's language/build system, writes `.github/workflows/*.yml` from the Phase 6 templates, validates the YAML locally |
| `repo-deployer` | `mcp__github__*`, `Read` | Branch, commit manifests and workflow files, open a PR, report the URL |
| `pipeline-doctor` | Actions read tools, `Read` | Reads a failed run's job logs, explains why it went red, proposes a workflow or manifest fix. Mutates nothing |

`events.py` maps SDK messages to SSE frames the browser understands: `StreamEvent` text deltas → `token`; `ThinkingBlock` → `thinking`; `ToolUseBlock` → `tool_call` (name + pretty-printed input); tool results → `tool_result` (collapsed, truncated); `ResultMessage` → `done` with `total_cost_usd` and turn count. Approval cards ride the same stream as `approval_request`.

### Phase 4 — FastAPI + UI (`main.py`, `static/`)

Endpoints:
- `GET /` → `static/index.html`
- `POST /api/chat` → `{session_id, message}`; returns `StreamingResponse(media_type="text/event-stream")` driving `client.query()` + `receive_response()`
- `POST /api/approve` → `{request_id, decision}`
- `POST /api/interrupt` → `client.interrupt()` (stop button)
- `GET /api/clusters` / `POST /api/clusters/select` → profile list + switch
- `GET /api/cluster/overview` → `kube.py` runs `kubectl get pods -A -o json` + recent warning events directly; drives a live side panel with no token cost

UI is three panes: cluster overview (auto-refresh ~10s), chat transcript with collapsible tool cards, and approval cards rendered inline with the exact command and Approve/Deny buttons. Tool calls render as compact rows (`kubectl_get pods -n demo`) that expand to full input/output.

### Phase 5 — CI/CD pipeline authoring (`workspace/templates/workflows/`)

**The constraint that shapes this:** GitHub-hosted runners live in GitHub's cloud and cannot reach a Docker Desktop cluster on this laptop — there is no route to it and no kubeconfig they could use. So a workflow cannot deploy locally, and pretending otherwise produces a pipeline that is permanently red. The split:

| Stage | Runs where | Now (local) | Later (EKS) |
|---|---|---|---|
| Build + push image | GitHub-hosted runner | ✅ | ✅ unchanged |
| Lint / validate manifests (`kubeconform`, `kubectl --dry-run`) | GitHub-hosted runner | ✅ | ✅ unchanged |
| Deploy | — | Local agent applies via k8s MCP, approval-gated | GitHub runner: OIDC → IAM role → `aws eks update-kubeconfig` → apply |

So generated workflows carry a `deploy` job that is **present but disabled** — guarded by `if: vars.DEPLOY_TARGET == 'eks'`, with the OIDC `permissions: id-token: write` block and the `aws-actions/configure-aws-credentials` step already written and commented where an account-specific value is needed. Turning on CD on EKS becomes: create the IAM role with a trust policy for the repo, set the `DEPLOY_TARGET` repo variable, fill the role ARN. No workflow rewrite, no new agent capability.

Templates the `pipeline-author` subagent works from (parameterized, not copy-pasted blindly — it reads the repo first and adapts):

- `build-and-validate.yml` — checkout → set up buildx → build → push to GHCR (`GITHUB_TOKEN` already has `packages: write`, so no secret to create) → `kubeconform` the manifests → `kubectl apply --dry-run=client`. This is the one that works today.
- `pr-validate.yml` — manifest lint + dry-run only, on `pull_request`. Cheap gate.
- `deploy-eks.yml` — the disabled job described above, kept as a separate template for repos that want CD split out.

Registry note: default to **GHCR** (`ghcr.io/<owner>/<repo>`) because the built-in `GITHUB_TOKEN` can push to it with no secret setup, which matters given the agent deliberately has no Secrets permission. ECR is the natural switch when EKS arrives, and is a documented variant in the template header.

The local half closes the loop: once the pipeline pushes an image tag, the agent updates the Deployment image in `workspace/manifests/` and applies it to `docker-desktop` behind an approval card. That is the "deploy" step for now, and it is the same declarative manifest the EKS job will later apply — so nothing gets thrown away.

`pipeline-doctor` handles the failure path: `list_workflow_runs` → find the red one → `get_job_logs` → explain → propose a fix. All read-only, so it runs without interrupting you; only committing the fix raises a card.

### Phase 6 — Seed a broken cluster for testing

`workspace/manifests/demo-broken.yaml` — namespace `demo` with three deliberately broken workloads: an `ImagePullBackOff` (nonexistent tag), a `CrashLoopBackOff` (`command: ["sh","-c","exit 1"]`), and a `Pending` pod (impossible resource request). This is the fixture the whole flow is demoed against.

---

## Verification

Run end-to-end in this order; each step gates the next.

1. **Cluster**: `kubectl get nodes` shows `docker-desktop` `Ready`.
2. **MCP handshake**: start the app, check the `system`/`init` message logs `k8s` and `github` with `status: "connected"`. A `failed` k8s server means the Windows `npx` spawn issue — apply the Phase 1 fallback.
3. **Read path (no prompts)**: `kubectl apply -f workspace/manifests/demo-broken.yaml`, then ask *"what's wrong in the demo namespace?"*. Expect tool cards for `kubectl_get`/`describe`/`logs` that run without any approval card, and a correct diagnosis of all three failures.
4. **Write path (approval)**: ask *"fix the crashlooping deployment"*. Expect an approval card showing the exact `kubectl_patch`/`kubectl_apply` input. Click **Deny** → agent reports it was blocked and does not retry blindly. Ask again, click **Approve** → pod goes `Running`, confirmed by `kubectl get pods -n demo`.
5. **Hard deny**: ask *"delete the kube-system namespace"*. Expect an immediate refusal with no approval card, and `kubectl get ns kube-system` still healthy.
6. **Manifest authoring**: *"write a Deployment + Service + ConfigMap for an nginx app with 2 replicas, resource limits and a readiness probe"*. Expect files in `workspace/manifests/`, a passing `--dry-run=client`, then an approval-gated apply.
7. **GitHub**: *"commit those manifests to `<owner>/<repo>` on branch `k8s-manifests` and open a PR"*. Expect approval cards for the write tools and a real PR URL at the end.
8. **Pipeline authoring**: *"add a CI pipeline to `<owner>/<repo>` that builds the image and validates the manifests"*. Expect `.github/workflows/build-and-validate.yml` committed via an approval card, then a run appearing in Actions and going **green** on a GitHub-hosted runner. If the commit 403s, the PAT is missing **Workflows: read/write** (Phase 0, step 3).
9. **Pipeline debugging**: deliberately break the workflow (bad action version or a manifest that fails `kubeconform`), push, then ask *"why did the last run fail?"*. Expect `list_workflow_runs` + `get_job_logs` to run with **no approval card**, a correct explanation, and a proposed fix that only prompts when it commits.
10. **Deploy loop closes locally**: after a green run, ask *"deploy that image to the demo namespace"*. Expect the manifest image tag updated in `workspace/manifests/` and an approval-gated apply to `docker-desktop`, with `kubectl get pods -n demo` showing the new image.
11. **Interrupt + resume**: fire a long query, hit Stop mid-tool-call, then send a follow-up — same session, context intact.
12. **AWS seam (dry check, no AWS account needed)**: add a bogus `eks-test` profile to `clusters.yaml`, switch to it in the UI, confirm the app rebuilds the MCP config with the new `K8S_CONTEXT`/`AWS_*` env and reports a clean connection error rather than crashing. Separately confirm the generated `deploy` job is skipped (not failed) while `DEPLOY_TARGET` is unset.

---

## Risks worth knowing up front

- **`kubectl_generic` is a full kubectl escape hatch** — it can express anything the policy tries to bucket. It is treated as WRITE and pattern-scanned, but a determined prompt could still surface something odd in an approval card. Read the card before clicking Approve; that is the control.
- **Cost**: every chat turn runs Opus 5 with cluster output in context. `kubectl get -A` on a large cluster is expensive in tokens; the side panel deliberately uses raw kubectl instead of the agent for routine viewing. If cost becomes an issue, moving `k8s-diagnostician` to `claude-sonnet-5` is a one-line change.
- **Secrets**: `MASK_SECRETS=true` masks `kubectl get secrets` output only — **not pod logs**. Logs from an app that prints credentials will reach the model.
- **This is a local single-user tool.** There is no authentication on the FastAPI app. Bind it to `127.0.0.1` and do not expose the port.
- **Classify tools by explicit allowlist, not prefix glob.** `get_*`/`list_*` reads well today, but a future GitHub MCP tool named something like `list_and_cancel_*` would auto-approve itself into the READ bucket. Enumerate the read tools by name and default *unknown* tools to WRITE (approval), never to READ.
- **An agent that writes its own CI is an agent that can change what runs on push.** A workflow file is executable code on GitHub's runners; approving a `.github/workflows/` commit is approving future automated execution, not just a file. Read those cards more carefully than manifest cards. This is exactly why the PAT has no Secrets permission — a workflow the agent wrote cannot be paired with a secret the agent created.
- **Local cluster is genuinely local.** Nothing about this setup lets GitHub reach into the machine. The trade is that "CD" today means *the agent applies it*, not *the pipeline applies it* — real end-to-end CD arrives with EKS.
