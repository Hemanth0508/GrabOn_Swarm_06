"""
Runtime_V2/providers/groq_provider.py
Groq provider — LLaMA 3 and Mistral via Groq API.

Agent 2 (Data Agent)   — LLaMA 3.1 8B Instant
Agent 3 (Report Agent) — Mistral Saba 24B

Both agents share one internal base class — the Groq SDK call
pattern is identical across models.
Key: GROQ_API_KEY

Falls back to deterministic simulation when no API key is set.
"""

import os

from runtime.agents.base import AgentBase
from runtime.agents.scanner import scan_response


# ── Shared Groq base ───────────────────────────────────────────────────────────

class _GroqBase(AgentBase):
    """
    Internal base for all Groq-hosted agents.
    Concrete subclasses declare MODEL; everything else is shared.
    All governance plumbing inherited from AgentBase unchanged.
    """

    MODEL:    str = ""
    PROVIDER: str = "groq"

    def __init__(self, session_id, principal_id, principal_type, private_key):
        super().__init__(
            session_id     = session_id,
            principal_id   = principal_id,
            principal_type = principal_type,
            model_name     = self.MODEL,
            api_provider   = self.PROVIDER,
            private_key    = private_key,
        )
        self._api_key = os.environ.get("GROQ_API_KEY", "")

    def run_task(self, task_description: str) -> str:
        if not self._api_key:
            return self._simulate_reasoning(task_description)

        try:
            from groq import Groq
            client = Groq(api_key=self._api_key)
            response = client.chat.completions.create(
                model    = self.model_name,
                messages = [
                    {"role": "system", "content": self._build_system_prompt()},
                    {"role": "user",   "content": task_description},
                ],
                max_tokens = 512,
            )
            raw = response.choices[0].message.content
            clean, _ = scan_response(
                session_id = self.session_id,
                tool       = "llm_output",
                raw_result = raw,
            )
            return clean
        except Exception as e:
            return (
                f"[{self.__class__.__name__} error: {e}] "
                f"Falling back to simulation."
            )

    def _simulate_reasoning(self, task: str) -> str:
        return (
            f"[{self.__class__.__name__}/{self.model_name}] "
            f"Task received: '{task[:80]}'. "
            f"Executing via governed tools."
        )


# ── Concrete Groq agents ───────────────────────────────────────────────────────

class LlamaProvider(_GroqBase):
    """
    LLaMA 3.1 8B Instant via Groq.
    Agent 2 — Data Agent.
    Drop-in replacement for LlamaAgent.
    """
    MODEL = "llama-3.1-8b-instant"


class MistralProvider(_GroqBase):
    """
    Mistral Saba 24B via Groq.
    Agent 3 — Report Agent.
    Drop-in replacement for MistralAgent.
    """
    MODEL = "mistral-saba-24b"