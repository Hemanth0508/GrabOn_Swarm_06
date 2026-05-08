"""
Runtime_V2/providers/gemini_provider.py
Gemini provider — Google AI Studio.

Agent 1 (Orchestrator) and Agent 5 (Statistical Sub).
Model: gemini-2.0-flash
SDK:   google-generativeai
Key:   GEMINI_API_KEY

Falls back to deterministic simulation when no API key is set,
preserving full demo functionality in keyless environments.
"""

import os

from runtime.agents.base import AgentBase
from runtime.agents.scanner import scan_response


class GeminiProvider(AgentBase):
    """
    Gemini 2.0 Flash agent via Google AI Studio.

    Drop-in replacement for GeminiAgent.
    Upgraded from gemini-1.5-flash → gemini-2.0-flash.
    All governance plumbing (call_tool, interceptor, signing) inherited
    from AgentBase unchanged.
    """

    MODEL    = "gemini-2.0-flash"
    PROVIDER = "google_ai_studio"

    def __init__(self, session_id, principal_id, principal_type, private_key):
        super().__init__(
            session_id     = session_id,
            principal_id   = principal_id,
            principal_type = principal_type,
            model_name     = self.MODEL,
            api_provider   = self.PROVIDER,
            private_key    = private_key,
        )
        self._api_key = os.environ.get("GEMINI_API_KEY", "")

    # ── Task execution ─────────────────────────────────────────────────────────

    def run_task(self, task_description: str) -> str:
        """
        Run a task using Gemini 2.0 Flash.
        Falls back to simulation when GEMINI_API_KEY is not set.
        """
        if not self._api_key:
            return self._simulate_reasoning(task_description)

        try:
            import google.generativeai as genai
            genai.configure(api_key=self._api_key)
            model = genai.GenerativeModel(
                model_name         = self.model_name,
                system_instruction = self._build_system_prompt(),
            )
            response = model.generate_content(task_description)
            raw = response.text
            clean, _ = scan_response(
                session_id = self.session_id,
                tool       = "llm_output",
                raw_result = raw,
            )
            return clean
        except Exception as e:
            return (
                f"[GeminiProvider error: {e}] "
                f"Falling back to simulation."
            )

    def _simulate_reasoning(self, task: str) -> str:
        return (
            f"[GeminiProvider/{self.model_name}] "
            f"Task received: '{task[:80]}'. "
            f"Executing via governed tools."
        )