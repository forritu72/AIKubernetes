# Code Walkthrough — how this app actually works

This document follows one user message all the way through the system and back,
naming every function that runs, in the order it runs. Then it goes file by file
and explains what each one is for and why it exists.

If you only read one section, read **[Part 2: The life of one message](#part-2-the-life-of-one-message)**.

- [Part 0: The 60-second version](#part-0-the-60-second-version)
- [Part 1: Startup](#part-1-startup-what-happens-before-anyone-types-anything)
- [Part 2: The life of one message](#part-2-the-life-of-one-message)
- [Part 3: File-by-file reference](#part-3-file-by-file-reference)
- [Part 4: The event protocol](#part-4-the-event-protocol-browser--server)
- [Part 5: Design decisions worth knowing](#part-5-design-decisions-worth-knowing)
- [Part 6: How to extend it](#part-6-how-to-extend-it)

---

## Part 0: The 60-second version

You have a chat box in a browser. You type *"why is my postgres pod crashing?"*.

1. The browser POSTs that text to a Python web server (**FastAPI**).
2. The server hands it to an **agent loop** — a `while` loop that calls a
   language model, and every time the model asks to run a tool, runs it and
   feeds the result back.
3. The "tools" are real things: `kubectl get`, `kubectl logs`, writing a YAML
   file, committing to GitHub. Read-only tools run instantly. **Anything that
   changes something stops and waits for you to click Approve** in the browser.
4. Everything the loop does — each token of text, each tool call, each result,
   each approval request — is pushed to the browser live over a single streaming
   HTTP response (**SSE**), so you watch it work in real time.

Three moving parts do the heavy lifting:

| Part | Where | What it is |
|---|---|---|
| The agent loop | `app/agent.py` | model → tools → model, until the model stops asking for tools |
| The safety gate | `app/policy.py` + `app/permissions.py` | classifies every tool call READ / WRITE / FORBIDDEN |
| The hands | `app/mcp_client.py` + `app/builtin_tools.py` | actually talks to Kubernetes, GitHub, and the filesystem |

```
Browser (static/app.js)
    |  POST /api/chat                          SSE events stream back
    v                                          ^
FastAPI (app/main.py)                          |
    |                                          |
    v                                          |
ChatSession.run_turn  ---->  _drive  ---->  _run_agent  (app/agent.py)
                                               |     ^
                          +--------------------+     |
                          |                          |
                          v                          |
                   _complete (call the LLM)          |
                          |                          |
                          v                          |
                   _invoke (one tool call)           |
                          |                          |
             +------------+------------+             |
             |            |            |             |
             v            v            v             |
        policy.py    MCP tools   builtin tools       |
        (gate)       (k8s/github) (Read/Write/Bash)  |
             |                                       |
             v                                       |
       permissions.py --> approval card ------------>+
                          (blocks on your click)
```

---

## Part 1: Startup — what happens before anyone types anything

### 1.1 `run.ps1`

The launcher. In order it:

1. Fails if there is no `.venv`.
2. Copies `.env.example` → `.env` if you have no `.env` yet.
3. `pip install -r requirements.txt` (quietly, every run — cheap when nothing changed).
4. Greps `APP_PORT` out of `.env`, defaulting to `8099`.
5. Runs `uvicorn app.main:app --host 127.0.0.1 --port <port>`.

`127.0.0.1` is deliberate: **there is no authentication anywhere in this app.**
Anyone who can reach the port can mutate your cluster.

### 1.2 Import time — `app/config.py` runs first

`uvicorn` imports `app.main`, which imports `app.config`, which does two things
immediately at module level:

```python
settings = Settings()                                    # reads .env
clusters = ClusterRegistry(PROJECT_ROOT / settings.clusters_file)   # reads clusters.yaml
```

- `Settings` is a `pydantic-settings` class. Every field is an env var
  (`OPENAI_API_KEY`, `AGENT_MODEL`, `MAX_TOOL_ROUNDS`, …) with a default. It reads
  `.env` automatically. So **the whole app's configuration surface is the field
  list in `config.py`** — if it isn't a field there, it isn't configurable.
- `ClusterRegistry.reload()` parses `clusters.yaml` into `ClusterProfile` objects
  and picks the `default:` one as active. A profile is just *(kubectl context,
  kubeconfig path, extra env vars)*. This is the "switch to AWS EKS" seam.

Three path constants come from here and are used everywhere:

```python
PROJECT_ROOT = <repo root>       # the anchor for every relative path in the app
WORKSPACE    = PROJECT_ROOT/workspace   # the only directory the agent may write to
STATIC_DIR   = PROJECT_ROOT/static
```

### 1.3 `app/main.py` — building the FastAPI app

Top of file calls `trace.configure_logging()`, which installs the console log
format and silences `httpx`/`httpcore` chatter.

Then `app = FastAPI(..., lifespan=lifespan)`. The `lifespan` async context
manager runs **once on boot** and:

- Raises immediately if neither `OPENAI_API_KEY` nor `OPENAI_BASE_URL` is set —
  a loud failure at startup instead of a confusing one on the first message.
- Logs the model, endpoint, active cluster profile, log level.
- Warns if `GITHUB_TOKEN` is missing (GitHub tools are then never registered).
- On shutdown: `await sessions.shutdown()`, which tears down every chat session
  and kills the MCP subprocesses.

Note what does **not** happen at startup: **no MCP server is spawned, no model
client is created.** That is all lazy, per chat session, on the first message.

### 1.4 The browser loads

`GET /` returns `static/index.html`. `/static/*` is mounted as a static
directory. The page loads `app.js`, which at the very bottom fires three
requests and starts a timer:

```js
loadClusters();                      // GET /api/clusters   -> fills the dropdown
loadStatus();                        // GET /api/status     -> model / GitHub chips
refreshOverview();                   // GET /api/cluster/overview -> pods, nodes, events
setInterval(refreshOverview, 10000); // ...and again every 10 seconds
```

`SESSION_ID = crypto.randomUUID()` is generated once per page load. That single
string is what ties the browser to a server-side `ChatSession`. Reloading the
page = new UUID = new session = fresh conversation.

**The cluster panel never goes through the agent.** `/api/cluster/overview` calls
`app/kube.py`, which shells out to `kubectl` directly. Polling your cluster
through an LLM every 10 seconds would be slow and would cost real money.

---

## Part 2: The life of one message

You type **"deploy postgres and create a table"** and press Enter.

### Step 1 — Browser: `send()` in `static/app.js`

```js
input.addEventListener("keydown", ...)  ->  send(input.value)
```

`send()`:
1. Guards against sending while `busy`.
2. `renderUser(text)` — draws your bubble.
3. `setBusy(true)` — disables Send, shows Stop.
4. `fetch("/api/chat", { session_id: SESSION_ID, message: text })`.
5. Does **not** await the whole response. It grabs `res.body.getReader()` and
   reads the stream chunk by chunk, splitting on `\n\n`, stripping the
   `data: ` prefix, `JSON.parse`-ing each frame, and calling `handleEvent(...)`.

That reader loop is the entire live-update mechanism. Everything you see appear
in the transcript is one `handleEvent` call.

### Step 2 — Server: `POST /api/chat` in `app/main.py`

```python
@app.post("/api/chat")
async def chat(body: ChatRequest) -> StreamingResponse:
    session = await sessions.get(body.session_id)
    return StreamingResponse(session.run_turn(body.message), media_type="text/event-stream")
```

- `sessions` is the module-level `SessionManager` in `agent.py`. `get()` returns
  the existing `ChatSession` for that UUID or creates one bound to the currently
  active cluster profile.
- `session.run_turn(message)` is an **async generator**. FastAPI streams
  whatever it yields. `X-Accel-Buffering: no` and `Cache-Control: no-cache` stop
  proxies from buffering it into one lump.

### Step 3 — `ChatSession.run_turn()` (`app/agent.py`)

This function is the plumbing, not the logic. In order:

1. **Turn lock.** If `self.turn_lock.locked()`, it yields one `error` frame and
   returns. One turn at a time per session.
2. **`await self.start()`** — the lazy, expensive, once-per-session setup:
   - `_client()` builds an `AsyncOpenAI` pointed at `OPENAI_BASE_URL` (or
     OpenAI proper).
   - `build_mcp_servers(profile)` (`mcp_config.py`) produces the server map.
   - `MCPHub(...).connect()` spawns the servers and discovers their tools.
   - `ToolBox(hub)` merges MCP tools + built-in tools + the `Task` tool into one
     registry.
   - `self.history = [{"role": "system", "content": DEVOPS_PROMPT}]`.
3. **Drains the queue** of anything stale from a previous turn.
4. **Emits `init` once per session** — model name, MCP server statuses, tool
   count. The browser uses this to show "MCP server not connected" if k8s failed
   to start.
5. **`self._pump = asyncio.create_task(self._drive(prompt))`** — the agent runs
   as a *background task*, not inline.
6. **Then loops on `self.queue`**, converting every `(event_type, data)` tuple
   into an SSE frame via `sse()` and yielding it, until it pulls the `_DONE`
   sentinel.

> **Why a queue and a separate task?** Because the agent needs to emit events
> from deep inside nested calls (`_invoke` → `permissions.py` → approval card)
> without those functions knowing anything about HTTP. Everything anywhere in
> the call tree can `await session.emit(...)`; `run_turn` is the only thing that
> knows what SSE is.

If the browser disappears mid-turn, this generator is cancelled →
`except asyncio.CancelledError` → the agent task is cancelled and every pending
approval is auto-denied, so the agent isn't left running against your cluster
unattended.

### Step 4 — `ChatSession._drive()`

The turn wrapper. It:

1. `trace.turn_start(...)` — prints the `== turn [a1b2c3d4] deploy postgres...` banner.
2. **`self._refresh_system_prompt()`** — rebuilds the system message as
   `DEVOPS_PROMPT + _workspace_inventory()`. `_workspace_inventory()` walks
   `workspace/` and lists every file with its size.

   > This exists because the agent otherwise cannot see its own past work and
   > will ask *you* to paste a manifest it wrote ten minutes ago. The listing is
   > only written back **when it changes**, so a steady workspace keeps the
   > system prompt byte-identical and the provider's prompt cache intact.

3. Appends `{"role": "user", "content": prompt}` to `self.history`.
4. **`self._trim_history()`** — if the history is over `HISTORY_TOKEN_BUDGET`
   (≈ chars/4), drops the oldest exchanges. It **only ever cuts at a `user`
   message**, because an `assistant` message carrying `tool_calls` and the
   `tool` messages answering it must stay together or the API rejects the whole
   request.
5. **Calls `_run_agent(...)`** with the orchestrator's settings.
6. Whatever happens, emits a `done` event carrying duration, round count, token
   counts, and dollar cost; then `_DONE` to release `run_turn`.

### Step 5 — `ChatSession._run_agent()` — **the actual agent loop**

This is the heart of the program. It is ~40 lines and it is the same function
the orchestrator and every subagent run through.

```python
for round_number in range(1, max_rounds + 1):
    text, calls = await self._complete(...)        # 1. ask the model
    messages.append({"role": "assistant", ...})    # 2. record what it said
    if not calls:
        return text, round_number                  # 3. no tools wanted -> done
    for call in calls:                             # 4. run each tool
        result, failed = await self._invoke(call, ...)
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": ...})
# loop exhausted:
messages.append({"role": "user", "content": "You have used all N tool rounds..."})
text, _ = await self._complete(..., tools=[])      # forced summary, no tools offered
```

Two details that matter:

- **`messages` is mutated in place.** When the orchestrator runs, `messages` *is*
  `self.history`, so the conversation persists across turns. When a subagent
  runs, `messages` is a fresh local list, so its work never pollutes the main
  history. Same function, two lifetimes, purely from what you pass in.
- **The round limit never just stops.** It injects a user message telling the
  model to stop and summarise, then does one final model call with `tools=[]`
  so it physically cannot call another tool. You get a report instead of silence.

### Step 6 — `ChatSession._complete()` — one model call

Builds the request:

```python
{"model": ..., "messages": ..., "temperature": ..., "stream": True,
 "stream_options": {"include_usage": True},
 "tools": [...], "parallel_tool_calls": False}
```

`parallel_tool_calls=False` is a safety choice: it keeps approval cards in a
sane order and stops the model firing a mutation alongside the read that
justifies it.

Then it consumes the stream:

- `delta.content` → appended to `parts`, and if `stream_text` is true, emitted as
  a `token` event so text appears in the browser word by word. **Subagents pass
  `stream_text=False`** — their chatter would interleave with the orchestrator's.
- `delta.tool_calls` → this is the fiddly bit. A tool call arrives **split across
  many chunks**: the id in one, the name in pieces, the JSON arguments a few
  characters at a time. So `slots[piece.index]` accumulates `id`, `name`, and
  `args` by string concatenation until the stream ends.
- `chunk.usage` → fed into the `Usage` accumulator for cost reporting.

Returns `(text, calls)`.

### Step 7 — `ChatSession._invoke()` — one tool call, gated

For each accumulated call, in order:

1. **Parse arguments.** `json.loads(call["args"])`. If that fails, the call never
   reaches the cluster — it emits a `tool_call` card marked "malformed
   arguments" and returns an error string telling the model exactly what was
   wrong so it can retry properly.
2. **Check the tool exists** in the `ToolBox`.
3. **`summary = _summary(name, arguments)`** — the human-readable one-liner
   (`kubectl apply -f workspace/manifests/pg.yaml`) produced by
   `policy.summarize()`. This is what you read on the approval card, so it has a
   hand-written case for basically every tool shape.
4. **`bucket = classify(name, arguments).bucket`** — for the log line and to know
   whether to print "waiting for approval…".
5. **Emit `tool_call`** → browser draws a collapsible card showing the arguments.
6. **`verdict = await self.gate(name, arguments)`** — the permission gate. See
   Step 8. This call can block for minutes.
7. If `Deny` → emit an error `tool_result` and **return the denial message as the
   tool's result**, so the model reads "the user denied this" and explains
   itself rather than silently retrying a variant.
8. If `Allow` → run it:
   - `name == "Task"` → `self._run_subagent(...)` (Step 9)
   - name in `builtin_tools.NAMES` → `builtin_tools.call(...)`
   - otherwise → `self.hub.call(...)` → MCP
9. Emit `tool_result` (truncated to 4 000 chars for the browser). The full result
   goes back to the model via `for_model()`, clipped at 12 000 chars with a
   head+tail and a note telling it to narrow the query instead of re-running.

### Step 8 — The permission gate (`app/permissions.py` + `app/policy.py`)

`self.gate` was built at session construction by `make_permission_handler(self)`.
It calls `classify(tool_name, tool_input)` and branches three ways.

`classify()` in `policy.py` runs in this order — and the order *is* the safety
model:

```
1. _check_forbidden(...)   -> FORBIDDEN wins over everything, even a READ tool
2. name in READ_TOOLS      -> READ
3. anything else           -> WRITE
```

**FORBIDDEN** (`_check_forbidden`) catches:
- `Bash` and `exec_in_pod` command strings matching `rm -rf`, `kubectl delete`,
  `helm uninstall`, fork bombs, `mkfs`/`dd if=…of=/dev/`. Both are arbitrary
  command escape hatches — one on your laptop, one inside a container — so both
  get the identical scan.
- Any *mutating* call that names `kube-system`, `kube-public`, or
  `kube-node-lease` anywhere (explicit `namespace` field, or `-n foo` inside a
  command string). Reading them is fine and useful; mutating them is never
  allowed.
- A delete spanning all namespaces (`--all-namespaces` / `-A` combined with a
  delete verb), and `kubectl delete namespace`.

Result: emit a `policy_denial` event (red card in the UI, no buttons) and return
`Deny(...)` with text that explicitly tells the model *"this is a hard policy
limit, do not retry a variation"*.

**READ** — `READ_TOOLS` is an **explicit list of exact tool names**, never a
prefix glob. The comment in the file explains why: a rule like `list_*` would
one day auto-approve a hypothetical `list_and_cancel_runs`. Returns `Allow()`
and the tool runs instantly, no card.

**WRITE** — everything else, *including every tool the policy has never heard
of*. This is the important default: an unknown tool asks, it never just runs.
Here the gate:

```python
request = registry.create(session_id, tool_name, summary, tool_input, reason)
await session.emit("approval_request", request.to_event())
approved = await asyncio.wait_for(request.future, timeout=APPROVAL_TIMEOUT_SECONDS)
```

`registry` (`app/approvals.py`) is a dict of `request_id → ApprovalRequest`, each
holding an `asyncio.Future[bool]`. **The `await` on that future is where the
whole agent turn stops.**

Meanwhile in the browser, `renderApproval()` draws the card with the exact
command and Approve/Deny buttons. Clicking calls:

```
POST /api/approve {request_id, decision}
    -> main.approve()
    -> registry.resolve(request_id, approved)
    -> request.future.set_result(True/False)
```

…which wakes the awaiting coroutine deep inside `_invoke`, and the turn resumes.
Three other things can settle it: a 600-second timeout (`Deny`, card shows
"Timed out"), clicking Stop (`/api/interrupt` → `registry.deny_all`), or the
browser disconnecting.

### Step 9 — Subagents (`_run_subagent`)

The `Task` tool is synthesised in `_task_schema()` and its description lists the
five specialists from `prompts.py`. When the model calls it:

1. **Depth check.** `if depth >= 1: return "Subagents cannot delegate further."`
   Exactly one level. No recursion.
2. Look up the `AgentDefinition` in `SUBAGENTS`.
3. Build a **brand-new two-message list**: the subagent's own system prompt plus
   the orchestrator's instruction. The subagent **cannot see the conversation** —
   which is why the `Task` schema insists the prompt must stand alone.
4. Call `_run_agent` recursively with:
   - `tool_names=definition.tools` — the exact allowlist of tools it is *offered*.
     `k8s-diagnostician` is handed read-only k8s tools and literally has no way
     to mutate anything.
   - `model=SUBAGENT_MODEL` (usually a cheaper one).
   - `max_rounds=MAX_SUBAGENT_TOOL_ROUNDS`, `stream_text=False`, `depth=1`.
5. Return **only the final report string** as the `Task` tool's result.

That last point is the cost control: a subagent can read 50 000 tokens of pod
logs in its own context, and the orchestrator's history grows by one paragraph.
Every tool call the subagent makes still goes through `_invoke` → the same
permission gate, so a subagent can raise approval cards too (they're tagged
`subagent` in the UI).

### Step 10 — Finishing

The model returns text with no tool calls → `_run_agent` returns →
`_drive` emits `done` with usage and cost → `_DONE` → `run_turn`'s loop breaks →
the async generator ends → FastAPI closes the HTTP response → the browser's
`reader.read()` returns `done: true` → `finally { setBusy(false); refreshOverview(); }`.

That last `refreshOverview()` is why the cluster panel updates the instant a
deploy finishes instead of waiting up to 10 seconds for the next poll.

### The whole thing on one line each

```
app.js send()
  -> POST /api/chat
  -> main.chat()
  -> sessions.get() -> ChatSession
  -> run_turn()            [turn lock, start(), event pump]
     -> start()            [AsyncOpenAI + MCPHub.connect() + ToolBox]  (first turn only)
     -> _drive()           [system prompt refresh, history append, trim]
        -> _run_agent()    [the loop]
           -> _complete()  [stream one model call -> text + tool calls]
           -> _invoke()    [per call]
              -> policy.classify()
              -> permissions.can_use_tool()   -> approval card -> awaits your click
              -> toolbox.execute()            -> MCPHub.call() | builtin_tools.call()
              -> _run_subagent()              -> _run_agent() at depth 1
        -> emits "done"
  <- SSE frames all the way back to handleEvent() in app.js
```

---

## Part 3: File-by-file reference

### `app/main.py` — the HTTP surface (157 lines)

Every endpoint in the app:

| Endpoint | What it does |
|---|---|
| `GET /` | serves `static/index.html` |
| `POST /api/chat` | the streaming turn. Body: `{session_id, message}` |
| `POST /api/approve` | settles one approval. `409` if it's no longer pending |
| `POST /api/interrupt` | Stop button — denies pending approvals, cancels the agent task |
| `POST /api/session/reset` | New session — closes the session, kills its MCP servers |
| `GET /api/clusters` | profile list for the dropdown |
| `POST /api/clusters/select` | switch profile; **resets every session** so MCP rebuilds with new env |
| `GET /api/cluster/overview` | live pods/nodes/events, straight from kubectl |
| `GET /api/status` | model, endpoint, GitHub on/off, active context |

It holds no logic beyond validation — everything is delegated to `sessions`,
`registry`, `clusters`, and `kube`.

### `app/config.py` — settings and cluster profiles (160 lines)

- `Settings` — every tunable, read from `.env`. Notable derived properties:
  `has_llm_key` (a base URL alone counts, because Ollama needs no key),
  `subagent_model_name` (blank → same as agent model), `file_root` (the
  confinement root for file tools; blank disables confinement entirely).
- `ClusterProfile` / `ClusterRegistry` — parse `clusters.yaml`, track the active
  profile, expand `~` in kubeconfig paths.

### `app/mcp_config.py` — what to spawn (122 lines)

Produces `{"k8s": {...}, "github": {...}}`, the map `MCPHub` consumes.

- **`resolve_launcher()`** is the most load-bearing function in the file, and the
  reason `npm install` is required. On Windows, `npx` *is* `npx.cmd`, and since
  Node 20 `child_process.spawn` refuses to execute a `.cmd` directly — it throws
  `EINVAL`. The documented `{"command": "npx", ...}` config therefore dies at
  spawn time, and the only symptom is an MCP server stuck at `status: "failed"`.
  So the default is `node node_modules/mcp-server-kubernetes/dist/index.js`,
  which has no shim and pins the version. Fallback: route npx through `cmd.exe`.
- **`cwd` is pinned to `PROJECT_ROOT`.** `kubectl_apply` takes a filename and
  resolves it *in the MCP server's process*, so without this the server and the
  agent's file tools disagree about what `workspace/manifests/app.yaml` means.
- **GitHub is omitted entirely** when no PAT is set, so the agent sees no GitHub
  tools rather than tools that 401 on every call.
- `GITHUB_TOOLSETS` includes `actions`, which is **not** in the server's default
  set — without it the CI/CD half of the app has no tools at all.

### `app/mcp_client.py` — the MCP client (325 lines)

The hardest file in the project, for one reason stated in its docstring:

> The stdio transport is built on anyio task groups, whose cancel scopes must be
> entered and exited by the **same task**. `connect()` and `aclose()` are called
> from different FastAPI request handlers.

So each server gets a dedicated long-running asyncio task
(`MCPServerConnection._run`) that holds `async with Client(...)` for its entire
life, and callers talk to it over `self._inbox`, an `asyncio.Queue`. `call()`
puts `(tool, args, future)` on the queue and awaits the future; `_serve()` pulls
items and fulfils them.

Other things worth knowing:

- **`_stderr_to_log()`** creates a real OS pipe (`os.pipe()`) — not a file-like
  object, because `subprocess` calls `fileno()` on it — and drains it into the
  logger from a daemon thread. Without it the MCP server's stderr writes straight
  to the console and interleaves *mid-line* with your trace output.
- **`start()` never raises.** A server that fails to start is recorded as
  `status: "failed"`, contributes zero tools, and shows up in the UI's `init`
  event. The app keeps working with whatever connected.
- **`MCPHub.qualify()`** renames every tool to `mcp__<server>__<tool>`. That
  naming is what makes `policy.py`'s allowlists work and what the UI strips for
  display.
- **`_warn_on_policy_drift()`** logs a warning at startup for any name in the
  READ allowlist that no connected server actually offers. The remote GitHub MCP
  server renames its tools from time to time (it once folded eight read tools
  into one `pull_request_read`). The failure is silent and in the *safe*
  direction — an unknown tool falls to WRITE — but it turns routine reads into
  clicks, so it's worth a log line rather than a mystery.
- **`_openai_schema()`** converts an MCP tool definition into an OpenAI
  function-tool definition, clipping the name at 64 chars (the API limit).

### `app/builtin_tools.py` — local tools (432 lines)

`Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `TodoWrite`. Plain Python, plus
one thing no SDK gives you: **every path is resolved and checked against
`settings.file_root`.**

- **`_resolve()`** is the security boundary. It accepts both spellings the model
  reaches for — `workspace/manifests/x.yaml` (repo-relative, how the prompts name
  it) and `manifests/x.yaml` (workspace-relative, how it thinks about its cwd) —
  resolves both, keeps only candidates inside the root, and prefers one that
  exists. A prompt-injected "read `~/.env`" fails loudly.
- **`_cwd()` returns `PROJECT_ROOT`, not `WORKSPACE`.** This is deliberate and
  was a real bug: relative paths must mean the same thing to `Write`, to a `Bash`
  `kubectl apply -f ...`, and to the MCP server. When they disagreed, the agent
  wrote to `workspace/workspace/manifests/` and then couldn't find its own file.
- **`_search_base()` returns the *workspace*** though — an unscoped `Grep` should
  not walk `.venv` and `node_modules`, which the agent pays for by the token.
- **`_not_found_message()`** lists the sibling files on a miss, turning "no such
  file" into a correctable mistake instead of a dead end.
- **`_grep` rejects match-everything patterns** (`.*`, `^`, …). One observed call
  pulled 23 000 characters of YAML into the context to answer "which manifests
  exist"; the error tells the model to use `Glob` instead.
- `SCHEMAS` / `HANDLERS` / `NAMES` at the bottom are the registry the `ToolBox`
  merges in.

### `app/policy.py` — classification (331 lines)

Covered in Step 8. The other half of the file is **`summarize()`** — a long
`if/elif` producing the one-line command shown on tool cards and approval cards.
It has a hand-written case per tool shape because the card is what you read
before clicking Approve, and `{"resourceType": "deployment", "name": "pg", ...}`
is not something you can safety-check at a glance.

One subtlety: `_as_command()` joins argv lists **with spaces**, because
JSON-encoding `["rm","-rf","/data"]` gives `"rm", "-rf", "/data"` and the comma
defeats every `\brm\s+-rf\b` pattern in the file.

### `app/permissions.py` — the gate (116 lines)

`make_permission_handler(session)` returns the `can_use_tool(name, input)`
closure described in Step 8. Returns `Allow()` or `Deny(message)`. The `Deny`
messages are written *at the model*: "do not retry a variation", "acknowledge the
denial and ask what they would prefer".

### `app/approvals.py` — the pending registry (79 lines)

A dict of `request_id → ApprovalRequest`, each with an `asyncio.Future[bool]`.
`create` / `resolve` / `discard` / `pending_for` / `deny_all`. `resolve` returns
`False` for an unknown or already-settled request, which is what produces the
`409` and the UI's "No longer pending".

### `app/prompts.py` — the system prompts (365 lines)

- **`DEVOPS_PROMPT`** — the orchestrator's instructions. Most of it is hard-won
  operational knowledge encoded as rules: always `--dry-run=server` (client-side
  only parses YAML and passes manifests the API server rejects); never hand-write
  a PersistentVolume on a single-node cluster; `storageClassName: ""` does not
  mean "default"; verify a pod is actually `Running 1/1` because `apply` returning
  `created` means nothing; Postgres folds unquoted identifiers to lowercase.
- **`SUBAGENTS`** — five `AgentDefinition`s, each with a `description` (which the
  orchestrator reads in the `Task` schema), a `prompt`, and a **`tools` list**:

  | Subagent | Tools | Can it mutate? |
  |---|---|---|
  | `k8s-diagnostician` | read-only k8s + Read/Glob/Grep | no, by construction |
  | `manifest-author` | file tools + Bash + `explain_resource` | writes files only |
  | `pipeline-author` | file tools + GitHub reads | writes files only |
  | `repo-deployer` | GitHub reads + branch/commit/PR writes | yes, gated |
  | `pipeline-doctor` | GitHub reads | no |

- **`ORCHESTRATOR_GITHUB_TOOLS`** — the four GitHub tools the orchestrator keeps.
  The GitHub server offers 46, whose schemas come to ~14k tokens — more than half
  of every request, on work that usually never touches GitHub. The rest are
  reachable only through subagents, which still carry their full sets.

### `app/agent.py` — the loop (684 lines)

Covered in Part 2. The classes:

- **`ToolBox`** — merges MCP schemas + built-in schemas + `Task` into one dict.
  `schemas(allowed)` narrows to a subagent's set, silently skipping names no
  connected server provides. `orchestrator_names()` is everything except the
  bulk of GitHub.
- **`ChatSession`** — one browser tab. Owns the MCP hub, the message history, the
  event queue, the turn lock, the permission gate.
- **`SessionManager`** (module-level `sessions`) — the dict of live sessions.
  `reset_all()` is what a cluster switch calls.

### `app/events.py` — SSE framing and money (91 lines)

- `sse(type, data)` → `data: {...}\n\n`.
- `truncate(text, 4000)` → what the browser is shown.
- `for_model(text)` → clipped to 12 000 chars with head + tail and an instruction
  to narrow the query. **This is the line item that used to dominate the bill**:
  one `kubectl get pods -A` re-sent on every subsequent round of a turn.
- `Usage` — accumulates prompt/completion/cached tokens across the whole turn,
  subagents included. `cost_usd()` uses the `PRICING` table; an unlisted model
  reports token counts with no dollar figure rather than a wrong one.

### `app/trace.py` — the console log (167 lines)

One consistent shape for every stage, so a failure can be diagnosed from the
terminal:

```
== turn   [a1b2c3d4]  deploy postgres and create a table
>> model  gpt-4.1-mini  msgs=2 tools=35
<< model  gpt-4.1-mini  2.1s  in=7812 out=88 tool_calls=1
>> tool   [WRITE] kubectl apply -f workspace/manifests/postgres-deployment.yaml
   |      waiting for approval...
<< tool   ok   mcp__k8s__kubectl_apply  0.4s  86 chars
== done   6.2s  3 rounds  in=23785 out=115 cached=15360  $0.0051
```

- `_redact()` blanks anything whose key looks like `password`/`token`/`secret`/…
- `payload()` forces output to ASCII, because the Windows console codepage is
  cp1252 and a stray box-drawing character from `kubectl` would raise
  `UnicodeEncodeError` *inside logging* and lose the line entirely.
- **Failed tool calls log at WARNING regardless of level** — a failure is the
  reason you're reading the log.

### `app/kube.py` — the live panel (180 lines)

Direct `kubectl` via `asyncio.create_subprocess_exec`, three commands in
parallel (`pods -A -o json`, warning `events`, `nodes`). No LLM involved.

`_pod_row()` has one real piece of logic: `status.phase` says `"Running"` long
after a container has started crash-looping, so it digs into
`containerStatuses[].state.waiting.reason` / `terminated.reason` and shows
`CrashLoopBackOff` / `ImagePullBackOff` instead.

### `static/` — the UI

- **`index.html`** — topbar (cluster dropdown + status chips), left cluster
  panel, chat transcript, composer. No framework, no build step.
- **`app.js`** — covered in Steps 1 and 10. `handleEvent()` is the switch that
  maps each SSE event type to a render function. `toolCards` is a
  `Map<tool_id, element>` so a `tool_result` can find the card its `tool_call`
  created.
- **`styles.css`** — plain CSS.

### `tests/`

Run them with `.\.venv\Scripts\python.exe -m tests.test_policy` etc.

| File | Covers |
|---|---|
| `test_policy.py` | 46 classification cases — READ/WRITE/FORBIDDEN |
| `test_permissions.py` | the approval gate: allow, deny, timeout |
| `test_agent_loop.py` | the loop with a **scripted stub model** — no API key, no network. Tool-call accumulation across streaming deltas, the gate, subagent isolation, the round limit, and that history trimming never orphans a `tool` message from its `tool_calls` |
| `test_builtin_tools.py` | file tools and the path confinement frames |

### `workspace/`

The agent's working directory and the file tools' confinement root.
`manifests/` holds generated YAML; `templates/workflows/` holds the GitHub
Actions templates `pipeline-author` starts from.

---

## Part 4: The event protocol (browser ← server)

Every frame is `data: {"type": "...", ...}\n\n`. `handleEvent()` in `app.js`
handles exactly these:

| Event | Emitted by | Fields | UI effect |
|---|---|---|---|
| `init` | `run_turn`, once per session | `model`, `mcp_servers`, `tools` | warns if an MCP server failed |
| `token` | `_complete` | `text` | appends to the assistant bubble |
| `thinking` | *(reserved)* | `text` | separate dim block |
| `tool_call` | `_invoke` | `id`, `tool`, `summary`, `input`, `subagent` | new collapsible card, tagged "running…" |
| `tool_result` | `_invoke` | `id`, `content`, `truncated`, `is_error` | fills that card, tags it done/error |
| `approval_request` | `permissions` | `request_id`, `tool`, `summary`, `reason`, `input` | approval card with buttons |
| `approval_resolved` | `permissions` | `request_id`, `decision` | replaces buttons with a verdict |
| `policy_denial` | `permissions` | `tool`, `summary`, `reason` | red "Blocked by policy" card |
| `done` | `_drive` | `num_turns`, `duration_ms`, `total_cost_usd`, tokens, `result` | the footer line |
| `error` | anywhere | `message` | red error card |

---

## Part 5: Design decisions worth knowing

**Why is the agent loop hand-written instead of using an SDK?**
The loop, tool registry, and subagent dispatch are ~300 lines in `agent.py`. In
exchange, the provider is a config line: OpenAI, Ollama, vLLM, LM Studio and
OpenRouter all speak the same Chat Completions API, so switching is
`OPENAI_BASE_URL` + `AGENT_MODEL` in `.env` and nothing else. The MCP half is the
official `mcp` package; the tools half is `builtin_tools.py`.

**Why does everything go through a queue instead of returning values?**
Because an approval card has to appear in the browser *while* a function seven
frames deep is blocked awaiting a click. `emit()` is available everywhere;
`run_turn` is the only thing that knows about HTTP.

**Why is READ an explicit list and everything else WRITE?**
Fail-safe defaults. A new MCP tool the policy has never seen raises a card — safe
but mildly annoying — rather than running silently. The live k8s server exposed
`exec_in_pod`, `kubectl_context`, and `kubectl_reconnect`, none of which the
design anticipated; all three correctly defaulted to WRITE.

**Why are `Bash` and `exec_in_pod` pattern-scanned when they're already WRITE?**
Because approval is a human clicking a summary line, and both can smuggle
`kubectl delete` past a name-based policy. Some things should never reach a card
at all.

**Why can subagents only nest one level?**
`if depth >= 1: return "Subagents cannot delegate further."` — a recursion guard
on something that would otherwise spend money exponentially.

**Why is history trimmed only at `user` messages?**
Because the API rejects a `tool` message whose corresponding `assistant`
`tool_calls` message is missing. `test_agent_loop.py` covers exactly this.

**Where does the money actually go?**
Tool schemas in the request (which is why GitHub's 46 tools are delegated away),
and tool *results* re-sent on every round of a turn (which is why `for_model()`
clips at 12k and why delegation exists at all).

### Known limits

- **No authentication.** Bound to `127.0.0.1`. Don't expose the port.
- **`MASK_SECRETS` masks `kubectl get secrets`, not pod logs.** An app that
  prints credentials will have them reach the model.
- **No end-to-end CD into the local cluster.** GitHub-hosted runners can't reach
  a Docker Desktop cluster on your laptop. Pipelines build, push and validate;
  the deploy is done locally by the agent behind an approval card.
- **GitHub MCP tool names drift.** A rename turns a read into an approval click
  until `policy.py` is updated. The startup warning tells you when.
- **An agent that writes CI can change what runs on push.** Approving a
  `.github/workflows/` commit approves future automated execution, not just a
  file. The PAT deliberately has no Secrets permission.

---

## Part 6: How to extend it

**Add a new local tool** — three edits in `app/builtin_tools.py`: write an
`async def _mytool(args) -> str`, add its JSON schema to `SCHEMAS`, add it to
`HANDLERS`. Then decide its bucket: add the name to `BUILTIN_READ_TOOLS` in
`policy.py` if it's read-only, or leave it out and it automatically requires
approval.

**Add a new MCP server** — add an entry to `build_mcp_servers()` in
`mcp_config.py`. Its tools appear as `mcp__<yourname>__<tool>` automatically.
Add the read-only ones to `policy.py`'s allowlist; the rest gate themselves.

**Add a new subagent** — add an `AgentDefinition` to `SUBAGENTS` in `prompts.py`.
The `Task` tool's schema and enum are generated from that dict, so nothing else
needs touching. Keep `tools` as small as the job allows — a tool it isn't offered
is a tool it cannot call.

**Point at a different model** — `.env`: `OPENAI_BASE_URL` + `AGENT_MODEL`. Add a
row to `PRICING` in `events.py` if you want dollar figures. Tool-calling
reliability is the thing that varies between providers, and this agent leans on
it heavily.

**Point at EKS** — `aws eks update-kubeconfig --name prod`, uncomment the
`eks-prod` profile in `clusters.yaml`, pick it in the dropdown. That tears down
every session and rebuilds the MCP servers with the new env. No code change —
`mcp-server-kubernetes` shells out to the local `kubectl`, so IAM exec-credential
auth is handled by the AWS CLI.

**Turn off the filesystem confinement** — `FILE_TOOLS_ROOT=` (empty) in `.env`.
Understand what you're doing: it lets a prompt-injected instruction read
anything your user account can read.

---

## Setting this up on another machine

```powershell
git clone <your-repo-url>
cd Kubernates_Manager

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm install                      # required - see mcp_config.py notes above

copy .env.example .env           # then put your OPENAI_API_KEY in it

# Docker Desktop -> Settings -> Kubernetes -> Enable Kubernetes
kubectl config current-context   # -> docker-desktop

.\run.ps1                        # -> http://127.0.0.1:8099
```

`.env` is gitignored and is **not** in the repo — that's on purpose. Copy
`.env.example` and fill in your own keys on each machine.
