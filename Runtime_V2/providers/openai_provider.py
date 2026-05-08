"""
Runtime_V2/providers/openai_provider.py
OpenAI provider — GPT-4o via OpenAI API.

Agent 4 — Compliance Agent.
Replaces GemmaAgent (Groq/Gemma2) with GPT-4o.
Model: gpt-4o
SDK:   openai
Key:   OPENAI_API_KEY

Falls back to deterministic simulation when no API key is set,
preserving full demo functionality in keyless environments.

Compliance Agent role:
    Evaluates merchant risk, protocol arbitration, and conflict
    resolution verdicts (Event 21). GPT-4o is chosen here for its
    stronger instruction-following on structured compliance tasks.
"""

import os
from runtime.agents.base import AgentBase
from runtime.agents.scanner import scan_response


class OpenAIProvider(AgentBase):
    """
    GPT-4o agent via OpenAI API.

    Drop-in replacement for GemmaAgent as the Compliance Agent.
    All governance plumbing (call_tool, interceptor, signing) is inherited
    from AgentBase unchanged — the provider only controls run_task().
    """

    MODEL    = "gpt-4o"
    PROVIDER = "openai"

    def __init__(self, session_id, principal_id, principal_type, private_key):
        super().__init__(
            session_id     = session_id,
            principal_id   = principal_id,
            principal_type = principal_type,
            model_name     = self.MODEL,
            api_provider   = self.PROVIDER,
            private_key    = private_key,
        )
        self._api_key = os.environ.get("OPENAI_API_KEY", "")

    # ── Task execution ─────────────────────────────────────────────────────────

    def run_task(self, task_description: str) -> str:
        """
        Run a task using GPT-4o.
        Falls back to simulation when OPENAI_API_KEY is not set.
        """
        if not self._api_key:
            return self._simulate_reasoning(task_description)

        try:
            from openai import OpenAI
            client = OpenAI(api_key=self._api_key)
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
                f"[OpenAIProvider error: {e}] "
                f"Falling back to simulation."
            )

    def _simulate_reasoning(self, task: str) -> str:
        return (
            f"[OpenAIProvider/{self.model_name}] "
            f"Task received: '{task[:80]}'. "
            f"Executing via governed tools."
        )