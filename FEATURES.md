# Features

Everything this app can actually do, grounded in the tools that are wired up and
verified against a live `docker-desktop` cluster and the GitHub MCP server.

Each capability is tagged with how it behaves when the agent reaches for it:

| Tag | Meaning |
|---|---|
| **R** | Runs immediately, no prompt. On the READ allowlist in `app/policy.py`. |
| **A** | Pauses and shows an approval card with the exact command. Blocks until you click. |
| **✗** | Hard-denied. Never reaches a card, never runs. |

Unknown tools default to **A**, never **R** — a tool the policy has never heard
of is something you get asked about.

---

## 1. Ask it things in plain language

A single-page chat console at `http://127.0.0.1:8099`. You describe the outcome;
it gathers evidence, proposes a change, and asks before touching anything.

Things it is built to handle:

- *"The payments pods are crashlooping, find out why and fix it."*
- *"Write a Deployment + Service + ConfigMap for this repo and open a PR."*
- *"Give this repo a CI pipeline that builds the image and validates the manifests."*
- *"Deploy Postgres, create a database called shop, and add a users table."*
- *"Why did the last Actions run go red?"*
- *"Scale the api deployment to 5 and restart the rollout."*

Answers stream token by token. Tool calls appear inline as cards showing the
exact command and its output.

---

## 2. Cluster observation (R)

All of this runs without asking, because none of it can change anything.

- **List and inspect any resource type** — pods, deployments, statefulsets,
  daemonsets, jobs, cronjobs, services, ingresses, configmaps, secrets, PVCs,
  PVs, nodes, namespaces, CRDs, and anything else the API server exposes
  (`kubectl_get`).
- **Describe a resource with its full event history** (`kubectl_describe`) —
  this is where scheduling failures, image pull errors and probe failures are
  explained.
- **Read pod logs**, including `previous: true` for a container that already
  crashed (`kubectl_logs`). The previous container's logs hold the actual error;
  the current one is usually empty.
- **Explain any resource type's schema** (`explain_resource`) and **list the
  available API resources** (`list_api_resources`) — so it can check a field
  exists rather than inventing one.
- **Check the connection is alive** (`ping`).

Reading `kube-system`, `kube-public` and `kube-node-lease` is allowed and
useful — CoreDNS logs explain a lot of cluster-wide failures. *Mutating* them is
hard-denied (see §10).

---

## 3. Diagnosis (R)

The `k8s-diagnostician` subagent handles log-heavy triage in its own context, so
a thousand lines of logs never enter the main conversation. It is read-only by
construction: 9 tools, none of which can mutate.

It root-causes:

- **CrashLoopBackOff** — previous-container logs, exit codes, restart counts
- **ImagePullBackOff / ErrImagePull** — bad tag, missing registry credentials,
  wrong registry host
- **Pending pods** — resource requests vs node allocatable, nodeSelector,
  affinity, taints, unbound PVCs, and PersistentVolume node-affinity mismatches
- **OOMKilled** — memory limits against actual usage
- **Failing readiness/liveness probes**
- **A Service that returns nothing** — selector/label mismatch, wrong targetPort
- **A stuck rollout** that never converges

It reports back in a fixed shape — root cause, the evidence lines that prove it,
and a concrete proposed fix — and is told not to speculate past its evidence.

---

## 4. Cluster mutation (A — every one raises an approval card)

- **Apply manifests** from a file or an inline string (`kubectl_apply`)
- **Create resources** from a file or subcommand (`kubectl_create`)
- **Patch fields** — strategic merge, JSON merge, or JSON patch (`kubectl_patch`)
- **Scale** deployments and statefulsets (`kubectl_scale`)
- **Manage rollouts** — restart, pause, resume, undo, status (`kubectl_rollout`)
- **Delete resources** by type, name, label or manifest (`kubectl_delete`)
- **Bulk clean up** problematic pods — Evicted, CrashLoopBackOff, Completed
  (`cleanup`)
- **Node maintenance** — cordon, drain, uncordon (`node_management`)
- **Port-forward** to a pod or service, and stop it (`port_forward`,
  `stop_port_forward`)
- **Run arbitrary kubectl** when no dedicated tool fits (`kubectl_generic`)
- **Switch kubectl context** (`kubectl_context`)
- **Reconnect** the API client after a cluster restart (`kubectl_reconnect`)

### Helm (A)

- **Install** a chart with custom values and repos (`install_helm_chart`)
- **Upgrade** an existing release (`upgrade_helm_chart`)
- **Uninstall** a release (`uninstall_helm_chart`)

Requires `helm` on PATH; without it these tools are dead weight.

### Working inside a running container (A)

`exec_in_pod` runs a command inside a container, which is how jobs that live
*inside* the workload get done:

- Create a database and its tables in a Postgres pod
- Inspect a config file the container actually loaded
- `curl` a service from inside the cluster network

The agent is instructed to wait for the pod to be Ready first, run one statement
per call, pass `-d <database>` on the second `psql` (the most common mistake),
and verify with a read before reporting success. It is also told that Postgres
folds unquoted identifiers to lower case, so `CREATE TABLE testTable` actually
creates `testtable`.

---

## 5. Manifest authoring (A on write)

The `manifest-author` subagent writes YAML into `workspace/manifests/` and
validates it. It does not apply — that stays with the orchestrator, behind a
card.

Can author: Deployments, StatefulSets, DaemonSets, Jobs, CronJobs, Services
(ClusterIP/NodePort/LoadBalancer), Ingresses, NetworkPolicies, ConfigMaps,
Secrets, HPAs, PodDisruptionBudgets, ResourceQuotas, LimitRanges,
ServiceAccounts, Roles and RoleBindings.

Manifests are production-shaped by default — resource requests and limits,
readiness and liveness probes, a `securityContext` that drops root, an explicit
update strategy, and labels that match the Service selector. Anything
deliberately left out gets explained.

**Validation is server-side.** It runs `kubectl apply --dry-run=server`, not
`--dry-run=client`. The client check only parses YAML and will report success on
a manifest the API server rejects — a `fsGroup` placed on a container instead of
the pod passes the client check and fails the real apply.

It is also told which `securityContext` fields live at which level (`fsGroup` is
pod-only; `allowPrivilegeEscalation`, `capabilities` and `readOnlyRootFilesystem`
are container-only), and never to ship a blank placeholder — an empty `values:`
becomes `null`, the API server accepts it, and the failure surfaces much later as
a pod that will not schedule.

### Storage

It checks `kubectl get storageclass` before writing storage YAML. If the cluster
has a default class — Docker Desktop ships `hostpath` — it writes a PVC and
nothing else and lets the provisioner do the work, rather than hand-rolling a
PersistentVolume with a `nodeAffinity` that has to be exactly right.

---

## 6. GitHub (R for reads, A for writes)

Needs a fine-grained PAT in `GITHUB_TOKEN`. With no token, GitHub tools are not
registered at all — the agent sees no GitHub tools rather than tools that 401 on
every call.

**Reads (R):** repository search and metadata, file contents, branches, commits,
tags, releases, labels, code search, commit search, issue search, PR search,
issue details and comments (`issue_read`), pull request details, diffs, files,
commits, reviews, review comments and check runs (`pull_request_read`), Actions
workflows, runs, jobs and artifacts (`actions_list`, `actions_get`), and job
logs (`get_job_logs`).

**Writes (A):** create a branch, create or update a file, push multiple files in
one commit, delete a file, open/update/merge a pull request, update a PR branch,
create and submit PR reviews, create and update issues and comments, add
sub-issues, create and fork repositories, and trigger/re-run/cancel Actions runs
(`actions_run_trigger`).

**Deliberately absent:** secrets management. The PAT is meant to be created
without the Secrets permission, so the agent cannot create a credential for a
workflow it wrote.

---

## 7. CI/CD pipelines

`pipeline-author` inspects a repo — language, build system, Dockerfile, ports,
existing workflows — and adapts one of the templates in
`workspace/templates/workflows/` rather than pasting a generic one:

| Template | Triggers on |
|---|---|
| `build-and-validate.yml` | push to `main`, manual dispatch |
| `pr-validate.yml` | PRs touching `manifests/**` or `.github/workflows/**` |
| `deploy-eks.yml` | completion of `build-and-validate`, manual dispatch |

Defaults to GHCR (`ghcr.io/<owner>/<repo>`), because the built-in `GITHUB_TOKEN`
with `packages: write` can push there and no secret has to be created.

`repo-deployer` then creates a branch, commits, and opens a PR — every write
behind a card. PR descriptions explain what changed and what a reviewer should
check, and say explicitly when the PR adds workflow files, because merging that
grants GitHub permission to run the code on future pushes.

`pipeline-doctor` reads a red Actions run: finds the failed run, finds the failed
job, reads the actual failing step's output, and distinguishes a build error from
a bad action version, a missing permission, a manifest that failed validation, or
a flake. Read-only — it proposes the fix, commits nothing.

**One structural limit:** GitHub-hosted runners cannot reach a Kubernetes cluster
on your laptop. Pipelines build, push and validate; the *deploy* is performed
locally by the agent behind an approval card. Generated EKS deploy jobs stay
guarded by `if: vars.DEPLOY_TARGET == 'eks'` so they are skipped rather than
permanently red.

---

## 8. Local file and shell tools

| Tool | | What it does |
|---|---|---|
| `Read` | R | Read a file, with line numbers and an offset/limit window |
| `Glob` | R | List files by glob pattern, newest first |
| `Grep` | R | Regex search across file contents |
| `Write` | A | Write a file, creating parent directories |
| `Edit` | A | Exact-string replacement in an existing file |
| `Bash` | A | Run a shell command, for `kubectl apply --dry-run=server` and similar |
| `TodoWrite` | R | The agent's own scratch plan; changes nothing outside the conversation |

**Paths anchor at the project root.** `workspace/manifests/app.yaml` means the
same thing to `Write`, to `Bash`, and to the k8s MCP server's `kubectl_apply -f`.
A bare `manifests/app.yaml` is accepted too.

**They are confined to `workspace/`.** Every path is resolved and checked against
`FILE_TOOLS_ROOT`, so a prompt-injected "read `~/.env`" fails loudly instead of
succeeding quietly. Set `FILE_TOOLS_ROOT=` empty to lift it.

**The agent knows what is already on disk.** A listing of `workspace/` is folded
into the system prompt each turn, so it reads its own manifests instead of asking
you to supply a file it wrote ten minutes earlier.

---

## 9. Live cluster panel (no tokens spent)

A side panel polling `kubectl` directly — not through the agent, because asking
an LLM to refresh a pod table every ten seconds would be slow and expensive.

- **Pod table** — namespace, name, ready count, status, restarts, age, and a
  health flag. Unhealthy pods sort to the top. The status column shows the
  container's *waiting reason* (`CrashLoopBackOff`, `ImagePullBackOff`) rather
  than the pod phase, which says `Running` long after a container started
  crash-looping.
- **Recent warning events** — the 25 newest, with object, reason, message, count
  and age.
- **Node list** — name, Ready condition, allocatable CPU and memory.
- **Connection state** — a clear error if `kubectl` cannot reach the cluster.

---

## 10. Safety model

Three buckets, in `app/policy.py`, checked before every single tool call —
including calls made by subagents.

**READ is an explicit allowlist of tool names, never a prefix glob.** A rule like
`get_*`/`list_*` reads fine today, but a future MCP tool named
`list_and_cancel_runs` would glob its way into auto-approval.

**Hard denials (✗)** — these never reach a card:

- `rm -rf` / `rm -fr` (recursive force delete)
- `kubectl delete` smuggled through `Bash`
- `helm uninstall` smuggled through `Bash`
- Fork bombs
- Raw disk writes (`mkfs`, `dd if=… of=/dev/…`)
- **Any mutation of `kube-system`, `kube-public` or `kube-node-lease`**
- **Any delete spanning all namespaces** (`--all-namespaces`, `-A`,
  `allNamespaces: true`)
- **Deleting a namespace wholesale** via `kubectl_generic`

`Bash` and `exec_in_pod` are both pattern-scanned, because both are
arbitrary-command escape hatches that can route around a name-based policy — one
on this machine, one inside a container.

A denial is reported back to the model with an instruction not to retry it or
route around it with a different tool, so it explains the block instead of
silently trying a variant.

### Approvals

Every mutating call pauses and shows a card with the exact command —
`kubectl scale deployment/api -n demo --replicas=3`, not "modify a deployment".
Nothing runs until you click. Cards auto-deny after `APPROVAL_TIMEOUT_SECONDS`
(default 600). **Stop** interrupts a running turn and denies every outstanding
card.

---

## 11. Multi-cluster

`clusters.yaml` holds cluster profiles; the UI has a dropdown. Switching tears
down every session and rebuilds the MCP servers against the new profile's
context, kubeconfig and environment — no code change, no restart.

This is the AWS seam. Moving to EKS is:

```powershell
aws eks update-kubeconfig --name prod
# then uncomment the eks-prod profile in clusters.yaml
```

`mcp-server-kubernetes` shells out to the local `kubectl`, so IAM
exec-credential auth is handled by the AWS CLI.

---

## 12. Cost control

The whole agent loop is this project's own code, so every lever is exposed:

- **`AGENT_MODEL` / `SUBAGENT_MODEL`** — any OpenAI-compatible endpoint. Point
  `OPENAI_BASE_URL` at Ollama, vLLM, LM Studio or OpenRouter. Giving the
  subagents a cheaper model than the orchestrator is the single biggest saving,
  because they generate the most tokens.
- **`HISTORY_TOKEN_BUDGET`** (default 60k) — older exchanges are dropped whole,
  never mid-tool-call.
- **`MAX_TOOL_ROUNDS` / `MAX_SUBAGENT_TOOL_ROUNDS`** — hard stops on a loop that
  will not converge. On hitting the cap the agent is asked to summarise rather
  than cut off mid-thought.
- **Tool results are clipped to 12k characters** before entering the history, so
  one `kubectl get pods -A` cannot be re-sent on every subsequent round.
- **The orchestrator does not carry the whole GitHub toolset.** Those 46 schemas
  are ~14k tokens — 61% of every request — on work that usually never touches
  GitHub. It keeps four and delegates the rest.
- **Delegation is itself a cost control** — a subagent reads logs in its own
  context and only its summary comes back.

Every reply footer shows that turn's token counts and dollar cost.

---

## 13. Watching it work

Every turn traces to the console. `LOG_LEVEL=DEBUG` (or `LOG_TOOL_IO=true` for
just the payloads) adds full tool arguments and results, the MCP servers' own
stderr, and every Bash command line.

```
== turn   [a1b2c3d4]  deploy postgres and create a table
>> model  gpt-4.1-mini  msgs=2 tools=35
<< model  gpt-4.1-mini  2.1s  in=7812 out=88 tool_calls=1
>> tool   [WRITE] kubectl apply -f workspace/manifests/postgres-deployment.yaml
   |      waiting for approval...
<< tool   ok   mcp__k8s__kubectl_apply  0.4s  86 chars
   |      deployment.apps/postgres created
== done   6.2s  3 rounds  in=23785 out=115 cached=15360  $0.0051
```

`[READ]`/`[WRITE]`/`[FORBIDDEN]` is the policy decision, so you can see why
something did or did not stop for approval. Failed calls log at WARNING with the
full error regardless of level. Credentials are redacted, and output is forced to
ASCII so a stray character from `kubectl` cannot throw inside logging and lose
the line.

---

## 14. HTTP API

The UI is a client of a plain REST + SSE API, so anything here is scriptable.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The single-page UI |
| `POST` | `/api/chat` | Send a message; returns an SSE stream |
| `POST` | `/api/approve` | Approve or deny a pending card |
| `POST` | `/api/interrupt` | Stop the running turn, deny outstanding cards |
| `POST` | `/api/session/reset` | Drop a session's history and MCP servers |
| `GET` | `/api/clusters` | List cluster profiles |
| `POST` | `/api/clusters/select` | Switch the active cluster |
| `GET` | `/api/cluster/overview` | Pods, warning events, nodes (direct kubectl) |
| `GET` | `/api/status` | Model, endpoint, cluster, GitHub availability |

SSE event types: `init`, `token`, `thinking`, `tool_call`, `tool_result`,
`approval_request`, `approval_resolved`, `policy_denial`, `done`, `error`.

---

## 15. Panic switches

Set in `.env` when you want the tool layer itself to refuse, on top of the
approval flow. Both blunt the agent permanently while set:

- `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS=true` — the k8s MCP server refuses
  destructive tools
- `ALLOW_ONLY_READONLY_TOOLS=true` — reads only

---

## Not included, deliberately

- **No GitHub Secrets management.** The agent must not be able to create
  credentials for the workflows it writes.
- **No end-to-end CD into a local cluster.** GitHub-hosted runners cannot reach
  a Docker Desktop cluster on your laptop.
- **No authentication on the app itself.** Bound to `127.0.0.1`; do not expose
  the port.
- **`MASK_SECRETS` masks `kubectl get secrets`, not pod logs.** An app that
  prints credentials will have them reach the model.

---

## Tool inventory

77 tools total, as discovered from the live servers:

| Source | Count | R | A |
|---|---|---|---|
| `mcp-server-kubernetes` | 23 | 6 | 17 |
| GitHub MCP | 46 | 25 | 21 |
| Local built-ins | 7 | 4 | 3 |
| `Task` (subagent dispatch) | 1 | 1 | — |

The orchestrator is offered 35 of these; the other 42 are GitHub tools reached
through subagents. Tool names and their READ/WRITE classification are checked at
startup, and `app/mcp_client.py` logs a warning naming any allowlisted tool that
no connected server offers — which is how an upstream rename gets noticed rather
than silently turning reads into approval prompts.
