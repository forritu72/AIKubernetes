/* Local DevOps Copilot - chat UI over the SSE agent stream. */

const SESSION_ID = crypto.randomUUID();

const $ = (id) => document.getElementById(id);
const transcript = $("transcript");
const input = $("input");
const sendBtn = $("send-btn");
const stopBtn = $("stop-btn");
const connDot = $("conn-dot");

let busy = false;
let currentBubble = null;   // assistant text element for the running turn
let thinkingEl = null;
const toolCards = new Map(); // tool_use_id -> <details>

/* ---------- helpers ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function atBottom() {
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 120;
}

function append(node, force) {
  const stick = force || atBottom();
  transcript.appendChild(node);
  if (stick) transcript.scrollTop = transcript.scrollHeight;
  return node;
}

function clearWelcome() {
  const welcome = transcript.querySelector(".welcome");
  if (welcome) welcome.remove();
}

function pretty(value) {
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function setBusy(state) {
  busy = state;
  sendBtn.disabled = state;
  stopBtn.classList.toggle("hidden", !state);
  connDot.className = "dot " + (state ? "busy" : "ok");
}

/* ---------- rendering ---------- */

function renderUser(text) {
  clearWelcome();
  const wrap = el("div", "msg user");
  wrap.appendChild(el("div", "who", "You"));
  wrap.appendChild(el("div", "bubble", text));
  append(wrap, true);
}

function assistantBubble() {
  if (!currentBubble) {
    const wrap = el("div", "msg assistant");
    wrap.appendChild(el("div", "who", "Copilot"));
    currentBubble = el("div", "bubble");
    wrap.appendChild(currentBubble);
    append(wrap);
  }
  return currentBubble;
}

function renderToolCall(data) {
  currentBubble = null;
  const card = el("details", "tool");
  const summary = el("summary");
  summary.appendChild(el("span", "tool-name", data.tool.replace(/^mcp__/, "").replace(/__/g, ".")));
  summary.appendChild(el("span", "tool-sum", data.summary || ""));
  const tag = el("span", "tag", data.subagent ? "subagent" : "running…");
  summary.appendChild(tag);
  card.appendChild(summary);

  const body = el("pre", null, pretty(data.input));
  card.appendChild(body);
  card._tag = tag;
  toolCards.set(data.id, card);
  append(card);
}

function renderToolResult(data) {
  const card = toolCards.get(data.id);
  const text = (data.content || "").trim() || "(no output)";
  const shown = data.truncated ? text + "\n\n… truncated" : text;

  if (!card) {
    const orphan = el("details", "tool" + (data.is_error ? " err" : ""));
    const summary = el("summary");
    summary.appendChild(el("span", "tool-name", "result"));
    orphan.appendChild(summary);
    orphan.appendChild(el("pre", null, shown));
    append(orphan);
    return;
  }
  if (data.is_error) card.classList.add("err");
  if (card._tag) card._tag.textContent = data.is_error ? "error" : "done";
  const out = el("pre", null, shown);
  card.appendChild(out);
  if (data.is_error) card.open = true;
}

function renderApproval(data) {
  currentBubble = null;
  const card = el("div", "approval");
  card.dataset.requestId = data.request_id;

  card.appendChild(el("div", "head", "Approval required"));
  card.appendChild(el("pre", "cmd", data.summary));
  card.appendChild(el("div", "why", data.reason || ""));

  const details = el("details");
  details.appendChild(el("summary", null, "Full tool input"));
  details.appendChild(el("pre", null, `${data.tool}\n\n${pretty(data.input)}`));
  card.appendChild(details);

  const actions = el("div", "actions");
  const approveBtn = el("button", "approve", "Approve");
  const denyBtn = el("button", "deny", "Deny");
  actions.appendChild(approveBtn);
  actions.appendChild(denyBtn);
  card.appendChild(actions);

  const decide = async (decision) => {
    approveBtn.disabled = denyBtn.disabled = true;
    try {
      const res = await fetch("/api/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: data.request_id, decision }),
      });
      if (!res.ok) {
        approveBtn.disabled = denyBtn.disabled = false;
        settleApproval(data.request_id, "expired");
      }
    } catch {
      approveBtn.disabled = denyBtn.disabled = false;
    }
  };
  approveBtn.onclick = () => decide("approve");
  denyBtn.onclick = () => decide("deny");

  append(card, true);
}

function settleApproval(requestId, decision) {
  const card = transcript.querySelector(`.approval[data-request-id="${requestId}"]`);
  if (!card || card.classList.contains("settled")) return;
  card.classList.add("settled");
  const actions = card.querySelector(".actions");
  if (actions) actions.remove();
  const labels = {
    approved: ["ok", "Approved - running"],
    denied: ["no", "Denied - not run"],
    timeout: ["no", "Timed out - not run"],
    expired: ["no", "No longer pending"],
  };
  const [cls, text] = labels[decision] || ["no", decision];
  card.appendChild(el("div", "verdict " + cls, text));
}

function renderDenial(data) {
  currentBubble = null;
  const card = el("div", "denial");
  card.appendChild(el("div", "head", "Blocked by policy"));
  card.appendChild(el("div", "cmd", data.summary || data.tool));
  card.appendChild(el("div", "why", data.reason || ""));
  append(card, true);
}

function renderDone(data) {
  currentBubble = null;
  thinkingEl = null;
  const line = el("div", "done-line");
  if (data.num_turns) line.appendChild(el("span", null, `${data.num_turns} turns`));
  if (typeof data.total_cost_usd === "number") {
    line.appendChild(el("span", null, `$${data.total_cost_usd.toFixed(4)}`));
  }
  if (data.duration_ms) line.appendChild(el("span", null, `${(data.duration_ms / 1000).toFixed(1)}s`));
  if (data.is_error) line.appendChild(el("span", null, `⚠ ${data.subtype}`));
  append(line);
}

function renderError(message) {
  currentBubble = null;
  const card = el("div", "denial");
  card.appendChild(el("div", "head", "Error"));
  card.appendChild(el("div", "why", message));
  append(card, true);
}

/* ---------- SSE turn ---------- */

function handleEvent(event) {
  switch (event.type) {
    case "token":
      assistantBubble().textContent += event.text;
      if (atBottom()) transcript.scrollTop = transcript.scrollHeight;
      break;
    case "thinking":
      if (!thinkingEl) {
        currentBubble = null;
        thinkingEl = append(el("div", "thinking"));
      }
      thinkingEl.textContent += event.text;
      break;
    case "tool_call":
      thinkingEl = null;
      renderToolCall(event);
      break;
    case "tool_result":
      renderToolResult(event);
      break;
    case "approval_request":
      renderApproval(event);
      break;
    case "approval_resolved":
      settleApproval(event.request_id, event.decision);
      break;
    case "policy_denial":
      renderDenial(event);
      break;
    case "init":
      reportMcp(event);
      break;
    case "done":
      renderDone(event);
      break;
    case "error":
      renderError(event.message);
      break;
  }
}

function reportMcp(event) {
  const failed = (event.mcp_servers || []).filter((s) => s.status && s.status !== "connected");
  if (failed.length) {
    renderError(
      "MCP server not connected: " +
        failed.map((s) => `${s.name} (${s.status}${s.error ? ": " + s.error : ""})`).join(", ") +
        ". Cluster tools will not work until this is fixed."
    );
  }
}

async function send(text) {
  if (busy || !text.trim()) return;
  renderUser(text);
  input.value = "";
  setBusy(true);
  toolCards.clear();
  currentBubble = null;
  thinkingEl = null;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: SESSION_ID, message: text }),
    });
    if (!res.ok || !res.body) {
      renderError(`Request failed: ${res.status} ${await res.text()}`);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        if (!frame.startsWith("data: ")) continue;
        try {
          handleEvent(JSON.parse(frame.slice(6)));
        } catch (err) {
          console.error("bad SSE frame", err, frame);
        }
      }
    }
  } catch (err) {
    renderError(String(err));
  } finally {
    setBusy(false);
    refreshOverview();
  }
}

/* ---------- cluster panel ---------- */

function renderOverview(data) {
  const banner = $("cluster-error");
  if (!data.connected) {
    banner.textContent = data.error || "cluster unreachable";
    banner.classList.remove("hidden");
    connDot.className = "dot err";
  } else {
    banner.classList.add("hidden");
    if (!busy) connDot.className = "dot ok";
  }

  const nodes = $("nodes");
  nodes.replaceChildren();
  (data.nodes || []).forEach((node) => {
    const row = el("div", "row " + (node.ready ? "good" : "bad"));
    row.appendChild(el("span", "name", node.name));
    row.appendChild(el("span", "state", node.ready ? "Ready" : "NotReady"));
    nodes.appendChild(row);
  });
  if (!(data.nodes || []).length) nodes.appendChild(el("div", "muted", "no nodes"));

  const pods = $("pods");
  pods.replaceChildren();
  (data.pods || []).forEach((pod) => {
    const row = el("div", "row " + (pod.healthy ? "good" : "bad"));
    const name = el("span", "name");
    name.appendChild(el("span", "ns", pod.namespace + "/"));
    name.appendChild(document.createTextNode(pod.name));
    row.appendChild(name);
    const restarts = pod.restarts ? ` ↻${pod.restarts}` : "";
    row.appendChild(el("span", "state", `${pod.ready} ${pod.status}${restarts}`));
    pods.appendChild(row);
  });
  if (!(data.pods || []).length) pods.appendChild(el("div", "muted", "no pods"));

  const total = (data.pods || []).length;
  $("pod-count").textContent = data.unhealthy ? `${data.unhealthy} unhealthy / ${total}` : `${total}`;

  const events = $("events");
  events.replaceChildren();
  (data.events || []).forEach((event) => {
    const card = el("div", "event");
    card.appendChild(el("div", "reason", `${event.reason} · ${event.object} · ${event.age}`));
    card.appendChild(el("div", "msg", event.message));
    events.appendChild(card);
  });
  if (!(data.events || []).length) events.appendChild(el("div", "muted", "no warnings"));

  $("overview-age").textContent = new Date().toLocaleTimeString();
}

async function refreshOverview() {
  try {
    const res = await fetch("/api/cluster/overview");
    renderOverview(await res.json());
  } catch (err) {
    console.error(err);
  }
}

async function loadClusters() {
  const res = await fetch("/api/clusters");
  const data = await res.json();
  const select = $("cluster-select");
  select.replaceChildren();
  data.profiles.forEach((profile) => {
    const option = el("option", null, profile.label);
    option.value = profile.name;
    option.selected = profile.name === data.active;
    select.appendChild(option);
  });
  const active = data.profiles.find((p) => p.name === data.active);
  $("context-chip").textContent = active ? active.context : "—";
}

async function loadStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  $("model-chip").textContent = data.model;
  const github = $("github-chip");
  github.textContent = "GitHub: " + (data.github ? "on" : "off");
  github.className = "chip " + (data.github ? "on" : "off");
}

/* ---------- wiring ---------- */

sendBtn.onclick = () => send(input.value);

stopBtn.onclick = async () => {
  await fetch("/api/interrupt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: SESSION_ID }),
  });
};

$("reset-btn").onclick = async () => {
  await fetch("/api/session/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: SESSION_ID }),
  });
  transcript.replaceChildren();
  location.reload();
};

$("cluster-select").onchange = async (e) => {
  await fetch("/api/clusters/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: e.target.value }),
  });
  await loadClusters();
  refreshOverview();
};

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send(input.value);
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
});

transcript.addEventListener("click", (e) => {
  const example = e.target.closest("[data-example]");
  if (example) send(example.dataset.example);
});

loadClusters();
loadStatus();
refreshOverview();
setInterval(refreshOverview, 10000);
