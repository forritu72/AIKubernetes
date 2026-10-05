"""Settings and cluster-profile loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = PROJECT_ROOT / "workspace"
STATIC_DIR = PROJECT_ROOT / "static"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM ---------------------------------------------------------------
    # Any OpenAI-compatible endpoint works: leave the base URL unset for
    # OpenAI proper, or point it at Ollama / vLLM / OpenRouter / Groq. The
    # agent loop is ours, so nothing here is provider-locked.
    openai_api_key: str = ""
    openai_base_url: str = ""
    agent_model: str = "gpt-4.1-mini"
    # Subagents do bounded, mechanical work; a cheaper model there is most of
    # the saving. Blank means "same as agent_model".
    subagent_model: str = ""
    temperature: float = 0.0
    # Hard stop on a runaway loop - each round is one model call.
    max_tool_rounds: int = 40
    max_subagent_tool_rounds: int = 25
    # Roughly how much conversation to carry forward. Older turns are dropped
    # whole; this is the single biggest lever on cost per message.
    history_token_budget: int = 60_000

    github_token: str = ""

    # --- diagnostics --------------------------------------------------------
    # INFO traces every tool call, its decision, timing and result size.
    # DEBUG adds the full arguments and results of each call, plus the raw
    # kubectl/psql output - verbose, but it is what you want when a pod will not
    # come up and you need to see exactly what the agent asked for.
    log_level: str = "INFO"
    # Log full tool arguments and results at INFO instead of DEBUG.
    log_tool_io: bool = False
    # Characters of a tool argument/result echoed into the log line.
    log_payload_chars: int = 400

    app_host: str = "127.0.0.1"
    app_port: int = 8099

    clusters_file: str = "clusters.yaml"
    approval_timeout_seconds: int = 600

    # Directory the file tools (Read/Write/Edit/Glob/Grep) and Bash are pinned
    # to. Blank disables the check and lets the agent touch the whole disk.
    file_tools_root: str = str(WORKSPACE)

    # k8s MCP transport: "stdio" (local node process) or "http" (already-running server)
    k8s_mcp_transport: str = "stdio"
    k8s_mcp_http_url: str = "http://localhost:3001/mcp"
    k8s_mcp_auth_token: str = ""
    k8s_mcp_command: str = ""
    k8s_mcp_args: str = ""

    allow_only_non_destructive_tools: str = ""
    allow_only_readonly_tools: str = ""

    @property
    def log_level_name(self) -> str:
        return (self.log_level or "INFO").strip().upper()

    @property
    def has_github(self) -> bool:
        return bool(self.github_token.strip())

    @property
    def has_llm_key(self) -> bool:
        # A local endpoint (Ollama, vLLM) needs no key, so a custom base URL is
        # enough on its own to count as configured.
        return bool(self.openai_api_key.strip() or self.openai_base_url.strip())

    @property
    def subagent_model_name(self) -> str:
        return self.subagent_model.strip() or self.agent_model

    @property
    def file_root(self) -> Path | None:
        root = self.file_tools_root.strip()
        return Path(root).resolve(strict=False) if root else None


settings = Settings()


@dataclass
class ClusterProfile:
    """One entry from clusters.yaml."""

    name: str
    label: str
    context: str
    kubeconfig: str
    env: dict[str, str] = field(default_factory=dict)

    @property
    def kubeconfig_path(self) -> str:
        """Absolute kubeconfig path, with ~ expanded."""
        return str(Path(os.path.expanduser(self.kubeconfig)).resolve(strict=False))


class ClusterRegistry:
    """Cluster profiles plus the currently selected one."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.profiles: dict[str, ClusterProfile] = {}
        self.default_name: str = ""
        self.active_name: str = ""
        self.reload()

    def reload(self) -> None:
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        profiles = raw.get("profiles") or {}
        if not profiles:
            raise ValueError(f"{self.path} defines no profiles")

        self.profiles = {
            name: ClusterProfile(
                name=name,
                label=(body or {}).get("label", name),
                context=(body or {}).get("context", ""),
                kubeconfig=(body or {}).get("kubeconfig", "~/.kube/config"),
                env={k: str(v) for k, v in ((body or {}).get("env") or {}).items()},
            )
            for name, body in profiles.items()
        }

        self.default_name = raw.get("default") or next(iter(self.profiles))
        if self.default_name not in self.profiles:
            raise ValueError(f"default profile '{self.default_name}' is not defined")
        if self.active_name not in self.profiles:
            self.active_name = self.default_name

    @property
    def active(self) -> ClusterProfile:
        return self.profiles[self.active_name]

    def select(self, name: str) -> ClusterProfile:
        if name not in self.profiles:
            raise KeyError(name)
        self.active_name = name
        return self.active


clusters = ClusterRegistry(PROJECT_ROOT / settings.clusters_file)
