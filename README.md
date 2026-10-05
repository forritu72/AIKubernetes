# Local DevOps Copilot

A locally hosted chat console that does DevOps work against a real Kubernetes
cluster and a real GitHub account. Reads run automatically; **every write pauses
for an approval click** in the UI.

Fully open-source stack: the agent loop, tool registry, and subagents are this
project's own code (`app/agent.py`), talking to any **OpenAI-compatible** endpoint
via the [`openai`](https://github.com/openai/openai-python) client and to MCP
servers via the official [`mcp`](https://github.com/modelcontextprotocol/python-sdk)
package. The hands are [`mcp-server-kubernetes`](https://github.com/flux159/mcp-server-kubernetes)
and the remote GitHub MCP server. No vendor agent SDK. See `PLAN.md` for the
design rationale.

---

## Setup

### 1. Python dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Put an `OPENAI_API_KEY` in `.env`
([console](https://platform.openai.com/api-keys)). Startup fails loudly if
neither a key nor a custom endpoint is configured.

Because the loop is ours, the provider is a config line, not a rewrite:

| Endpoint | `OPENAI_BASE_URL` | `AGENT_MODEL` |
|---|---|---|
| OpenAI (default) | *(blank)* | `gpt-4.1-mini` |
| Ollama — free, local | `http://localhost:11434/v1` | `qwen2.5-coder:14b` |
| vLLM / LM Studio | `http://localhost:8000/v1` | whatever you serve |
| OpenRouter | `https://openrouter.ai/api/v1` | any model it fronts |

A local endpoint needs no key. Tool-calling reliability is the thing that varies
between them — this agent leans on it heavily, so a small local model will call
fewer tools correctly than `gpt-4.1-mini` does.

### 2. Node dependencies — **required, and easy to skip**

```powershell
npm install
```

This installs `mcp-server-kubernetes` locally. It is not optional convenience:

> **Windows / Node 20+ gotcha.** `npx` on Windows is `npx.cmd`, and since Node 20
> `child_process.spawn` refuses to execute a `.cmd` file directly — it throws
> `EINVAL`. The documented `{"command": "npx", "args": ["-y", ...]}` MCP config
> therefore dies at spawn time, and the only symptom you see is an MCP server
> stuck at `status: "failed"` with `CONNECTION_CLOSED`.
>
> So `app/mcp_config.py` launches the locally installed package with `node`
> directly, which has no shim involved and pins the version besides. If
> `node_modules/` is missing it falls back to routing npx through `cmd.exe`.

### 3. Cluster

Docker Desktop → Settings → Kubernetes → **Enable Kubernetes** → Apply & Restart.

```powershell
kubectl config current-context   # -> docker-desktop
kubectl get nodes                # -> one Ready node
```

Optionally `winget install Helm.Helm` — without it, the server's Helm tools are
dead weight.

### 4. GitHub (optional; the app runs without it)

Create a **fine-grained PAT** and put it in `.env` as `GITHUB_TOKEN`:

| Permission | Level | Why |
|---|---|---|
| Contents | Read/write | Commit manifests and workflow files |
| Pull requests | Read/write | Open PRs |
| **Workflows** | **Read/write** | **Required to commit anything under `.github/workflows/`. Without it the push 403s with a confusing message. Most common thing to get wrong.** |
| Actions | Read/write | Read run logs; trigger/re-run (approval-gated) |
| Issues | Read | Context when investigating |

Leave **Secrets unset on purpose** — the agent must not be able to create
credentials for the workflows it writes.

With no token set, GitHub tools are not registered at all, so the agent sees no
GitHub tools rather than tools that 401 on every call.

### 5. Run

```powershell
.\run.ps1          # -> http://127.0.0.1:8099
```

---

## Cost control

The knobs that actually matter, all in `.env`:

- **`SUBAGENT_MODEL`** — the specialists do bounded, mechanical work. Pointing
  them at a cheaper model than the orchestrator is the single biggest saving,
  because they generate the most tokens.
- **`HISTORY_TOKEN_BUDGET`** (default 60k) — how much conversation is carried
  forward. Older exchanges are dropped whole, never mid-tool-call.
- **`MAX_TOOL_ROUNDS`** / **`MAX_SUBAGENT_TOOL_ROUNDS`** — hard stops on a loop
  that will not converge.
- Tool results are clipped to 12k characters before they enter the history
  (`app/events.py`), so one `kubectl get pods -A` cannot be re-sent on every
  subsequent round of the turn.
- Delegation is a cost control, not just tidiness: a subagent reads logs in its
  own context and only its summary comes back.

Every reply footer shows that turn's token counts and, for a model in the price
table in `app/events.py`, the dollar cost.

---

## Watching it work

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
something did or did not stop for approval. Failed tool calls log at WARNING
with the full error regardless of level — a failure is the reason you are
reading the log. Anything that looks like a credential is redacted, and output
is forced to ASCII so a stray box-drawing character from `kubectl` cannot throw
inside logging and lose the line.

---

## Safety model

Three buckets, in `app/policy.py`:

| Bucket | Behavior |
|---|---|
| **READ** | Runs immediately. An explicit allowlist of tool *names*. |
| **WRITE** | Raises an approval card showing the exact command; blocks until you click. |
| **FORBIDDEN** | Hard-denied. Never reaches a card, never runs. |

Choices worth knowing:

- **READ is enumerated, never glob-matched.** A prefix rule like `get_*`/`list_*`
  reads fine today, but a future MCP tool named `list_and_cancel_runs` would glob
  its way into auto-approval. Unknown tools always fall to WRITE.
- This is not hypothetical: the live server exposes `exec_in_pod`,
  `kubectl_context`, and `kubectl_reconnect`, none of which the design document
  anticipated. All three correctly defaulted to WRITE.
- **`Bash` and `exec_in_pod` are both pattern-scanned**, because both are
  arbitrary-command escape hatches that can smuggle `kubectl delete` past a
  name-based policy — one on this machine, one inside a container.
- **Reading `kube-system` is allowed; mutating it is hard-denied.** CoreDNS logs
  explain a lot of cluster-wide failures, and reading them is harmless.
- A denial is reported back to the model with an instruction not to retry or
  route around it, so it explains the block instead of silently trying a variant.

Run the tests:

```powershell
.\.venv\Scripts\python.exe -m tests.test_policy        # 46 classification cases
.\.venv\Scripts\python.exe -m tests.test_permissions   # the approval gate
.\.venv\Scripts\python.exe -m tests.test_agent_loop    # the loop, offline
.\.venv\Scripts\python.exe -m tests.test_builtin_tools # file tools + path frames
```

`test_agent_loop` replaces the model with a scripted stub, so it needs no API
key and no network. It covers tool-call accumulation across streaming deltas,
the gate, subagent isolation, the round limit, and — the fiddly one — that
history trimming never orphans a `tool` message from the `tool_calls` that
produced it, which the API rejects outright.

---

## Layout

```
app/
  config.py        settings + clusters.yaml loader
  mcp_config.py    MCP server map per cluster profile (Windows spawn handling)
  mcp_client.py    MCP client: spawns servers, discovers tools, dispatches calls
  builtin_tools.py Read/Write/Edit/Glob/Grep/Bash/TodoWrite, workspace-confined
  policy.py        READ / WRITE / FORBIDDEN classification
  trace.py         console tracing for a turn (model, tools, timing, cost)
  approvals.py     pending-approval registry (one Future per request)
  permissions.py   the permission gate
  prompts.py       orchestrator prompt + 5 specialist subagents
  agent.py         the agent loop, subagent dispatch, session + history
  events.py        SSE framing, result clipping, token/cost accounting
  kube.py          direct kubectl for the live panel (no tokens spent)
  main.py          FastAPI endpoints
static/            single-page UI, no build step
workspace/         agent cwd - generated manifests, workflow templates
```

## The AWS seam

`clusters.yaml` is the only thing that changes to drive EKS instead:

```powershell
aws eks update-kubeconfig --name prod
# then uncomment the eks-prod profile in clusters.yaml
```

`mcp-server-kubernetes` shells out to the local `kubectl`, so IAM exec-credential
auth is handled by the AWS CLI. Switching profiles in the UI tears every session
down and rebuilds the MCP servers with the new env — no code change.

## Known limits

- **No end-to-end CD into the local cluster.** GitHub-hosted runners cannot reach
  a Docker Desktop cluster on this laptop. Pipelines build, push, and validate;
  the *deploy* is performed locally by the agent behind an approval card.
  Generated deploy jobs stay guarded by `if: vars.DEPLOY_TARGET == 'eks'` so they
  are skipped rather than permanently red.
- **`MASK_SECRETS` masks `kubectl get secrets`, not pod logs.** An app that prints
  credentials will have them reach the model.
- **No authentication.** Bound to `127.0.0.1`; do not expose the port.
- **Paths anchor at the project root; the workspace is the boundary.** Relative
  paths resolve from the project root, so `workspace/manifests/app.yaml` means
  the same thing to `Write`, to `Bash`, and to the k8s MCP server's
  `kubectl_apply -f` — they disagreed once, and the agent wrote a manifest to
  `workspace/workspace/manifests/` and then could not find it. A bare
  `manifests/app.yaml` is also accepted.
- **The agent is told what is already on disk.** A listing of `workspace/` is
  folded into the system prompt each turn, because otherwise the agent cannot
  see its own files and will ask you to supply a manifest it wrote ten minutes
  earlier. It is only rewritten when the listing actually changes, so a steady
  workspace keeps the prompt cache intact.
- **The orchestrator does not carry the whole GitHub toolset.** Those 46 schemas
  are ~14k tokens — 61% of every request — on work that usually never touches
  GitHub. It keeps four of them and delegates the rest to the subagents, which
  still hold their full sets.
- **Validation is server-side.** The prompts tell the agent to use
  `kubectl apply --dry-run=server`. `--dry-run=client` only checks that the YAML
  parses and will report success on a manifest the API server rejects — a
  `fsGroup` placed on a container instead of the pod passes the client check and
  fails the real apply.
- **The file tools are confined to `workspace/`.** `Read`/`Write`/`Edit`/`Glob`/
  `Grep` resolve every path and reject anything outside `FILE_TOOLS_ROOT`, so a
  prompt-injected "read `~/.env`" fails loudly. Set `FILE_TOOLS_ROOT=` (empty) to
  lift it.
- **GitHub MCP tool names drift.** The remote server has already folded 18 read
  tools into dispatchers like `pull_request_read` and `actions_list`. The
  allowlist in `policy.py` is name-based, so a rename means a read starts asking
  for approval — safe, but annoying. `app/mcp_client.py` logs a warning at
  startup naming any allowlisted tool no connected server offers.
- **An agent that writes CI can change what runs on push.** Approving a
  `.github/workflows/` commit approves future automated execution, not just a
  file. The PAT deliberately has no Secrets permission, so a workflow the agent
  wrote cannot be paired with a secret the agent created.
