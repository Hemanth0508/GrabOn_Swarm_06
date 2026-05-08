"""
Runtime_V2/providers/loader.py
Provider loader â€” resolves agent roles to provider classes.

Single entry point for agent construction throughout the runtime.
`load_agent()` replaces direct instantiation of GeminiAgent / LlamaAgent /
MistralAgent providers in five_agent_demo.py and setup_demo().

Agent composition:
    orchestrator  â†’ GeminiProvider   (gemini-2.0-flash,     Google AI Studio)
    data          â†’ LlamaProvider    (llama-3.1-8b-instant,  Groq)
    report        â†’ MistralProvider  (mistral-saba-24b,      Groq)
    compliance    â†’ MistralProvider  (mistral-saba-24b,      Groq)
    statistical   â†’ GeminiProvider   (gemini-2.0-flash,      Google AI Studio)

Design rules:
    - The runtime calls load_agent(role, ...) â€” it never names a provider directly.
    - Adding or swapping a provider requires only editing PROVIDER_REGISTRY here.
    - No provider module is imported at module load time; imports are deferred
      inside load_agent() to avoid SDK import errors when keys are absent.
    - All returned agents are fully governed AgentBase subclasses.
      The loader does not touch governance logic.
"""


from __future__ import annotations
from typing import TYPE_CHECKING
from dotenv import load_dotenv
load_dotenv()

if TYPE_CHECKING:
    from ..base import AgentBase


# â”€â”€ Role constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Use these instead of raw strings when calling load_agent().

ROLE_ORCHESTRATOR = "orchestrator"
ROLE_DATA         = "data"
ROLE_REPORT       = "report"
ROLE_COMPLIANCE   = "compliance"
ROLE_STATISTICAL  = "statistical"


# â”€â”€ Provider registry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Maps role name â†’ (module path, class name).
# Deferred import: the module is only loaded when that role is first requested.

_PROVIDER_REGISTRY: dict[str, tuple[str, str]] = {
    ROLE_ORCHESTRATOR: ("providers.gemini_provider", "GeminiProvider"),
    ROLE_DATA:         ("providers.groq_provider",   "LlamaProvider"),
    ROLE_REPORT:       ("providers.groq_provider",   "MistralProvider"),
    ROLE_COMPLIANCE:   ("providers.groq_provider",   "MistralProvider"),
    ROLE_STATISTICAL:  ("providers.gemini_provider", "GeminiProvider"),
}

_ROLE_FAILOVER_CANDIDATES: dict[str, list[tuple[str, str]]] = {
    ROLE_ORCHESTRATOR: [
        ("providers.gemini_provider", "GeminiProvider"),
        ("providers.groq_provider",   "LlamaProvider"),
    ],
    ROLE_DATA: [
        ("providers.groq_provider",   "LlamaProvider"),
    ],
    ROLE_REPORT: [
        ("providers.groq_provider",   "MistralProvider"),
    ],
    ROLE_COMPLIANCE: [
        ("providers.groq_provider",   "MistralProvider"),
    ],
    ROLE_STATISTICAL: [
        ("providers.gemini_provider", "GeminiProvider"),
        ("providers.groq_provider",   "MistralProvider"),
    ],
}


class _FailoverAgent:
    def __init__(self, role: str, agents: list):
        self._role = role
        self._agents = agents
        self._active_idx = 0

    def _is_degraded_output(self, text: str) -> bool:
        if not isinstance(text, str):
            return True
        s = text.strip()
        if not s:
            return True
        lowered = s.lower()
        if "falling back to simulation" in lowered:
            return True
        if s.startswith("[GeminiProvider/") and "Task received:" in s:
            return True
        if s.startswith("[LlamaProvider/") and "Task received:" in s:
            return True
        if s.startswith("[MistralProvider/") and "Task received:" in s:
            return True
        return False

    def run_task(self, task_description: str) -> str:
        reasons: list[str] = []
        requested_agent = self._agents[0]
        for idx, agent in enumerate(self._agents):
            provider_label = f"{agent.__class__.__name__}/{getattr(agent, 'model_name', 'unknown')}"
            try:
                output = agent.run_task(task_description)
            except Exception as exc:
                reasons.append(f"{provider_label}: exception={type(exc).__name__}")
                continue

            if self._is_degraded_output(output):
                reasons.append(f"{provider_label}: degraded_output")
                continue

            self._active_idx = idx
            _emit_provider_execution(
                role=self._role,
                requested_provider=_provider_label(requested_agent),
                actual_provider=_provider_label(agent),
                model=getattr(agent, "model_name", "unknown"),
                live_api_call=True,
            )
            if idx > 0:
                return f"[FAILOVER role={self._role} provider={provider_label}] {output}"
            return output

        primary = self._agents[0]
        simulated = primary._simulate_reasoning(task_description)
        reason = "; ".join(reasons) if reasons else "unknown"
        _emit_provider_execution(
            role=self._role,
            requested_provider=_provider_label(requested_agent),
            actual_provider=_provider_label(primary),
            model=getattr(primary, "model_name", "unknown"),
            live_api_call=False,
        )
        return f"[FALLBACK role={self._role} reason={reason}] {simulated}"

    def __getattr__(self, name):
        return getattr(self._agents[self._active_idx], name)


def _provider_label(agent) -> str:
    cls = agent.__class__.__name__
    if cls == "GeminiProvider":
        return "Gemini Flash"
    if cls == "LlamaProvider":
        return "Groq (Llama)"
    if cls == "MistralProvider":
        return "Groq (Mistral)"
    if cls == "OpenAIProvider":
        return "OpenAI"
    return cls


def _is_degraded_output(text: str) -> bool:
    if not isinstance(text, str):
        return True
    s = text.strip()
    if not s:
        return True
    lowered = s.lower()
    if "falling back to simulation" in lowered:
        return True
    if s.startswith("[GeminiProvider/") and "Task received:" in s:
        return True
    if s.startswith("[LlamaProvider/") and "Task received:" in s:
        return True
    if s.startswith("[MistralProvider/") and "Task received:" in s:
        return True
    if s.startswith("[OpenAIProvider/") and "Task received:" in s:
        return True
    if s.startswith("[FALLBACK role="):
        return True
    return False


def _emit_provider_execution(
    role: str,
    requested_provider: str,
    actual_provider: str,
    model: str,
    live_api_call: bool,
) -> None:
    print("[PROVIDER EXECUTION]")
    print(f"Agent Role: {role}")
    print(f"Requested Provider: {requested_provider}")
    print(f"Actual Provider Used: {actual_provider}")
    print(f"Model: {model}")
    print(f"Live API Call: {'TRUE' if live_api_call else 'FALSE'}")


class _ObservedAgent:
    """
    Diagnostic wrapper for single-provider roles.
    Emits explicit provider execution facts on every run_task() call.
    """
    def __init__(self, role: str, requested_agent, actual_agent):
        self._role = role
        self._requested_agent = requested_agent
        self._actual_agent = actual_agent

    def run_task(self, task_description: str) -> str:
        out = self._actual_agent.run_task(task_description)
        _emit_provider_execution(
            role=self._role,
            requested_provider=_provider_label(self._requested_agent),
            actual_provider=_provider_label(self._actual_agent),
            model=getattr(self._actual_agent, "model_name", "unknown"),
            live_api_call=not _is_degraded_output(out),
        )
        return out

    def __getattr__(self, name):
        return getattr(self._actual_agent, name)

# â”€â”€ Public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def load_agent(
    role:           str,
    session_id:     str,
    principal_id:   str,
    principal_type: str,
    private_key,
) -> "AgentBase":
    """
    Construct and return a governed agent for the given role.

    Args:
        role:           One of the ROLE_* constants defined above.
        session_id:     Session ID bound to this agent instance.
        principal_id:   Principal ID of the agent.
        principal_type: Principal type string (e.g. "AGENT").
        private_key:    Ed25519PrivateKey â€” required for interceptor signing.

    Returns:
        A fully initialised AgentBase subclass ready for call_tool() and run_task().

    Raises:
        ValueError: If role is not in the provider registry.
        ImportError: If the provider's SDK dependency is missing and simulation
                     is not available (should not occur â€” all providers fall back).
    """
    if role not in _PROVIDER_REGISTRY:
        available = ", ".join(sorted(_PROVIDER_REGISTRY))
        raise ValueError(
            f"Unknown agent role '{role}'. "
            f"Available roles: {available}"
        )    import importlib

    candidates = _ROLE_FAILOVER_CANDIDATES.get(role, [_PROVIDER_REGISTRY[role]])
    agents = []
    for module_path, class_name in candidates:
        module = importlib.import_module(module_path)
        provider_cls = getattr(module, class_name)
        agents.append(
            provider_cls(
                session_id     = session_id,
                principal_id   = principal_id,
                principal_type = principal_type,
                private_key    = private_key,
            )
        )

    if len(agents) == 1:
        return _ObservedAgent(role=role, requested_agent=agents[0], actual_agent=agents[0])
    return _FailoverAgent(role=role, agents=agents)

def available_roles() -> list[str]:
    """Return the list of registered agent roles."""
    return sorted(_PROVIDER_REGISTRY.keys())


def provider_info() -> list[dict]:
    """
    Return provider metadata for all registered roles.
    Useful for display in startup banners or diagnostics.

    Returns list of dicts: role, model, provider, api_key_env
    """
    import importlib

    _KEY_ENV = {
        "GeminiProvider":  "GEMINI_API_KEY",
        "LlamaProvider":   "GROQ_API_KEY",
        "MistralProvider": "GROQ_API_KEY",
        "OpenAIProvider":  "OPENAI_API_KEY",
    }

    rows = []
    for role in (ROLE_ORCHESTRATOR, ROLE_DATA, ROLE_REPORT,
                 ROLE_COMPLIANCE, ROLE_STATISTICAL):
        module_path, class_name = _PROVIDER_REGISTRY[role]
        try:
            module = importlib.import_module(module_path)
            cls    = getattr(module, class_name)
            model  = getattr(cls, "MODEL", "unknown")
            prov   = getattr(cls, "PROVIDER", "unknown")
        except Exception:
            model = "unavailable"
            prov  = "unavailable"

        import os
        key_env = _KEY_ENV.get(class_name, "")
        key_set = bool(os.environ.get(key_env, ""))

        rows.append({
            "role":        role,
            "class":       class_name,
            "model":       model,
            "provider":    prov,
            "api_key_env": key_env,
            "key_set":     key_set,
        })

    return rows

