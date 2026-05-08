"""
runtime/agents/base.py
Agent Governance v2 — Agent Base Class

Every agent in the system extends AgentBase.
Every tool call goes through call_tool() which:
    1. Calls the interceptor (validate())
    2. On ALLOWED: calls the actual tool, scans the response
    3. On BLOCKED: raises GovernanceBlock
    4. On PENDING: raises GovernancePending

No tool is ever contacted without passing through the interceptor.
No tool response is ever returned to the agent without passing through the scanner.

Model agents:
    GeminiAgent  — Gemini Flash via Google AI Studio (free)
    LlamaAgent   — LLaMA 3 via Groq (free)
    MistralAgent — Mistral via Groq (free)
    GemmaAgent   — Gemma via Groq (free)
"""

import json
import os
from abc import ABC, abstractmethod
from base64 import b64encode
from typing import Any, Optional

from runtime.interceptor.validate import (
    validate,
    verify_capability_token,
    InterceptorDecision,
)
from runtime.constraints.store import get_constraint
from .scanner import scan_response
from runtime.monitor.heartbeat import record_heartbeat
from runtime.identity.principals import sign_message as _sign_message


# ── Governance exceptions ──────────────────────────────────────────────────────

class GovernanceBlock(Exception):
    """Raised when the interceptor blocks a tool call."""
    def __init__(self, reason: str, decision: InterceptorDecision):
        super().__init__(reason)
        self.reason   = reason
        self.decision = decision


class GovernancePending(Exception):
    """Raised when the interceptor returns PENDING (awaiting human approval)."""
    def __init__(self, approval_id: str, decision: InterceptorDecision):
        super().__init__(f"Awaiting approval: {approval_id}")
        self.approval_id = approval_id
        self.decision    = decision


class GovernanceEscalate(Exception):
    """Raised when the interceptor returns ESCALATE for orchestrator recovery."""
    def __init__(self, escalation_reason: str, decision: InterceptorDecision):
        super().__init__(escalation_reason)
        self.escalation_reason = escalation_reason
        self.decision = decision


# ── Tool registry — simulated tools for the demo ──────────────────────────────
# In production these would be real API clients.
# Each tool callable receives (action, metadata, capability_token, session_id, tool_name).
#
# DEFENSE IN DEPTH — tool-level token enforcement:
# Every tool independently calls verify_capability_token() before executing.
# This means even if call_tool() is bypassed (e.g. tool called directly),
# the tool itself will reject the call if no valid interceptor-issued token is present.
# This is the true final enforcement boundary — not call_tool(), not the agent.
#
# Token structure: {'token': str, 'action_id': str, 'expires': str, 'principal_id': str}

def _verify_tool_token(capability_token: dict, session_id: str,
                       tool_name: str, action: str) -> None:
    """
    Verify capability token inside a tool function.
    Raises RuntimeError if token is missing, expired, or tampered.
    This runs BEFORE any tool logic — no token, no execution.
    """
    if not capability_token:
        raise RuntimeError(
            f"[{tool_name}] No capability token provided — "
            f"tool call must pass through the governance interceptor"
        )
    ok = verify_capability_token(
        token=capability_token["token"],
        session_id=session_id,
        tool=tool_name,
        action=action,
        action_id=capability_token["action_id"],
        token_expires=capability_token["expires"],
        principal_id=capability_token.get("principal_id", ""),
    )
    if not ok:
        raise RuntimeError(
            f"[{tool_name}] Capability token invalid or expired — "
            f"interceptor approval required before tool execution"
        )


def _tool_database(action: str, metadata: dict, capability_token: dict, session_id: str, tool_name: str) -> dict:
    _verify_tool_token(capability_token, session_id, tool_name, action)
    if action == "query_records":
        return {"records": [{"id": 1, "name": "Q4 Report", "value": 92.4}],
                "count": 1, "source": "database"}
    if action == "query_pii_table":
        return {"employees": [
            {"id": "E-1042", "name": "Alice Chen", "salary": 145000, "band": "L5"},
            {"id": "E-1043", "name": "Bob Kumar",  "salary": 132000, "band": "L4"},
        ], "pii": True, "classification": "CONFIDENTIAL"}
    return {"result": f"database/{action} executed", "metadata": metadata}

def _tool_slack_api(action: str, metadata: dict, capability_token: dict, session_id: str, tool_name: str) -> dict:
    _verify_tool_token(capability_token, session_id, tool_name, action)
    channel = metadata.get("channel", "#general")
    message = metadata.get("message", "")
    return {"posted": True, "channel": channel,
            "message_id": "msg-001", "preview": message[:60]}

def _tool_budget_spend(action: str, metadata: dict, capability_token: dict, session_id: str, tool_name: str) -> dict:
    _verify_tool_token(capability_token, session_id, tool_name, action)
    amount = metadata.get("amount", 0)
    return {"processed": True, "amount": amount,
            "transaction_id": f"txn-{hash(str(metadata)) % 100000:05d}",
            "status": "confirmed"}

def _tool_sensitive_data(action: str, metadata: dict, capability_token: dict, session_id: str, tool_name: str) -> dict:
    _verify_tool_token(capability_token, session_id, tool_name, action)
    return {"records": [
        {"employee_id": metadata.get("employee_id", "E-1042"),
         "record_type": metadata.get("record_type", "salary"),
         "value": 145000, "classification": "SENSITIVE"}
    ]}

def _tool_reauth_check(action: str, metadata: dict, capability_token: dict, session_id: str, tool_name: str) -> dict:
    _verify_tool_token(capability_token, session_id, tool_name, action)
    credential = metadata.get("credential", "")
    valid = bool(credential) and not credential.startswith("fake")
    return {"authenticated": valid, "principal": "verified",
            "method": "token"}

def _tool_email_api(action: str, metadata: dict, capability_token: dict, session_id: str, tool_name: str) -> dict:
    _verify_tool_token(capability_token, session_id, tool_name, action)
    return {"sent": True, "to": metadata.get("to", ""),
            "subject": metadata.get("subject", ""), "email_id": "email-001"}

TOOL_REGISTRY: dict[str, Any] = {
    "database":      _tool_database,
    "slack_api":     _tool_slack_api,
    "budget_spend":  _tool_budget_spend,
    "sensitive_data": _tool_sensitive_data,
    "reauth_check":  _tool_reauth_check,
    "email_api":     _tool_email_api,
}


# ── AgentBase ─────────────────────────────────────────────────────────────────

class AgentBase(ABC):
    """
    Base class for all governed agents.

    Every agent has a session_id, principal_id, principal_type, and private_key.
    The private_key (Ed25519PrivateKey) is used to sign every validate() call —
    the interceptor's auth pre-check requires a valid Ed25519 signature in
    metadata["signature"] proving the caller controls the claimed principal.

    Key storage:
        In-memory only for this demo (key is not persisted across restarts).
        In production: load from HSM or encrypted key store at agent startup.

    All tool calls are routed through call_tool() which:
        1. Signs the request with the agent's private key
        2. Calls validate() — all 7 checks + auth pre-check
        3. Verifies the returned capability token
        4. On ALLOWED: calls the actual tool, scans the response
        5. On BLOCKED: raises GovernanceBlock
        6. On PENDING: raises GovernancePending
    """

    def __init__(
        self,
        session_id:      str,
        principal_id:    str,
        principal_type:  str,
        model_name:      str,
        api_provider:    str,
        private_key,                    # Ed25519PrivateKey — required for signing
    ):
        self.session_id      = session_id
        self.principal_id    = principal_id
        self.principal_type  = principal_type
        self.model_name      = model_name
        self.api_provider    = api_provider
        self._private_key    = private_key  # Ed25519PrivateKey — never logged or exposed
        self._last_version   = 0  # tracks caller_constraint_version for check 4

    def call_tool(
        self,
        tool_name:        str,
        action:           str,
        metadata:         dict = None,
        idempotency_key:  Optional[str] = None,
        parent_action_id: Optional[str] = None,
    ) -> dict:
        """
        Execute a tool call through the governance interceptor.

        Step 1: Sign the request — inject Ed25519 signature into metadata.
        Step 2: Call validate() — auth pre-check + all 7 checks.
        Step 3: On BLOCKED → raise GovernanceBlock (tool never contacted).
        Step 4: On PENDING → raise GovernancePending (tool not contacted).
        Step 5: Verify capability token — confirm interceptor issued the approval.
        Step 6: On ALLOWED → call the actual tool.
        Step 7: Scan the tool response (scanner_required is always True on ALLOWED).
        Step 8: Return the (possibly sanitized) response.

        Args:
            tool_name:        Name of the tool (e.g. 'database', 'slack_api').
            action:           Action to execute (e.g. 'query_records').
            metadata:         Parameters for the tool call.
            idempotency_key:  Optional deduplication key for retry safety.
            parent_action_id: action_id of the causing action (causal graph).

        Returns:
            Tool result dict with governance metadata injected.

        Raises:
            GovernanceBlock:   If interceptor blocks the action.
            GovernancePending: If interceptor returns PENDING.
            ValueError:        If tool_name is not in the tool registry.
        """
        if metadata is None:
            metadata = {}

        # ── Step 1: Sign the request ───────────────────────────────────────────
        # The interceptor's auth pre-check (before Check 1) requires a valid
        # Ed25519 signature over the message: session_id:tool:action:metadata_json
        # This proves the caller controls the claimed principal.
        # NOTE: signature is computed over the metadata WITHOUT the signature field
        # itself (that would be circular). The interceptor replicates this convention.
        auth_message = (
            f"{self.session_id}:{tool_name}:{action}:"
            f"{json.dumps(metadata, sort_keys=True)}"
        ).encode()
        signature_b64 = _sign_message(self._private_key, auth_message)
        metadata["signature"] = signature_b64

        # ── Step 2: Interceptor enforcement ───────────────────────────────────
        decision = validate(
            session_id=self.session_id,
            claimed_principal=self.principal_id,
            claimed_principal_type=self.principal_type,
            tool=tool_name,
            action=action,
            metadata=metadata,
            idempotency_key=idempotency_key,
            caller_constraint_version=self._last_version,
            parent_action_id=parent_action_id,
        )

        # Update caller's version tracking for read-your-writes consistency
        self._last_version = decision.constraint_version_at_decision

        if decision.result == "BLOCKED":
            raise GovernanceBlock(decision.reason, decision)

        if decision.result == "PENDING":
            raise GovernancePending(decision.approval_id, decision)

        if decision.result == "ESCALATE":
            raise GovernanceEscalate(
                decision.escalation_reason or decision.reason,
                decision,
            )

        # ── Step 5: Verify capability token ───────────────────────────────────
        # Confirm the interceptor issued this approval and the token has not expired.
        # This is the tool-layer enforcement boundary — prevents interceptor bypass.
        if decision.capability_token:
            token_data = decision.capability_token
            token_ok = verify_capability_token(
                token=token_data["token"],
                session_id=self.session_id,
                tool=tool_name,
                action=action,
                action_id=token_data["action_id"],
                token_expires=token_data["expires"],
                principal_id=token_data.get("principal_id", self.principal_id),
            )
            if not token_ok:
                raise GovernanceBlock(
                    "capability_token verification failed — interceptor approval "
                    "expired or token tampered; tool execution refused",
                    decision,
                )

        # ── Step 6: Tool execution (ALLOWED + token verified) ─────────────────
        tool_fn = TOOL_REGISTRY.get(tool_name)
        if tool_fn is None:
            raise ValueError(
                f"Tool '{tool_name}' not found in tool registry. "
                f"Available: {sorted(TOOL_REGISTRY.keys())}"
            )

        raw_result = tool_fn(
            action,
            metadata,
            decision.capability_token,
            self.session_id,
            tool_name,
        )

        # ── Step 7: Response path scanning (MANDATORY — scanner_required=True) ─
        # scanner_required is always True on ALLOWED decisions.
        # Scanning is not optional — skipping it would leave the injection
        # protection layer open. AgentBase enforces it unconditionally.
        record_heartbeat(
            session_id=self.session_id,
            tool=tool_name,
            action=action,
            tool_result=raw_result,
        )

        clean_result, was_blocked = scan_response(
            session_id=self.session_id,
            tool=tool_name,
            raw_result=raw_result,
        )

        # Inject governance metadata into result
        if isinstance(clean_result, dict):
            clean_result["_governance"] = {
                "action_id":  decision.action_id,
                "result":     decision.result,
                "scanned":    True,
                "sanitized":  was_blocked,
                "version":    decision.constraint_version_at_decision,
            }

        return clean_result

    @abstractmethod
    def run_task(self, task_description: str) -> str:
        """
        Run a natural language task using this agent's model.
        Must use call_tool() for all tool interactions.
        """
        pass

    def _build_system_prompt(self) -> str:
        """Standard governance-aware system prompt injected into every agent."""
        return (
            f"You are {self.model_name}, an enterprise AI agent operating under "
            f"strict governance constraints. Your session is bound to principal "
            f"{self.principal_id} with type {self.principal_type}. "
            f"You must use only the tools provided. "
            f"Every tool call is intercepted by a governance runtime — "
            f"violations are blocked at infrastructure level regardless of your reasoning."
        )


# ── Model agent implementations ────────────────────────────────────────────────

    def _get_state_summary(self) -> str:
        """Read verified governance state from the constraint store."""
        pii_accessed = get_constraint(self.session_id, "pii_accessed")
        reauth_verified = get_constraint(self.session_id, "reauth_verified")
        budget_limit = get_constraint(self.session_id, "budget_limit")
        budget_spent = get_constraint(self.session_id, "budget_spent")
        return (
            "Verified Governance State:\n"
            f"- session_id: {self.session_id}\n"
            f"- principal_id: {self.principal_id}\n"
            f"- principal_type: {self.principal_type}\n"
            f"- pii_accessed: {pii_accessed}\n"
            f"- reauth_verified: {reauth_verified}\n"
            f"- budget_limit: {budget_limit}\n"
            f"- budget_spent: {budget_spent}\n"
        )

    def _ground_task(self, task_description: str) -> str:
        """Prepend verified governance state before each reasoning step."""
        return f"{self._get_state_summary()}\nTask:\n{task_description}"


class GeminiAgent(AgentBase):
    """
    Gemini Flash agent via Google AI Studio (free tier).
    Uses the google-generativeai SDK.
    Falls back to simulated reasoning if API key not set.
    """

    def __init__(self, session_id, principal_id, principal_type, private_key):
        super().__init__(
            session_id=session_id,
            principal_id=principal_id,
            principal_type=principal_type,
            model_name="gemini-1.5-flash",
            api_provider="google_ai_studio",
            private_key=private_key,
        )
        self._api_key = os.environ.get("GEMINI_API_KEY", "")

    def run_task(self, task_description: str) -> str:
        """
        Run a task using Gemini Flash reasoning.
        In demo mode (no API key), uses deterministic task simulation.
        """
        if not self._api_key:
            return self._simulate_reasoning(task_description)

        try:
            import google.generativeai as genai
            genai.configure(api_key=self._api_key)
            model = genai.GenerativeModel(
                model_name=self.model_name,
                system_instruction=self._build_system_prompt(),
            )
            response = model.generate_content(task_description)
            raw = response.text
            clean, _ = scan_response(
                session_id=self.session_id,
                tool="llm_output",
                raw_result=raw,
            )
            return clean
        except Exception as e:
            return f"[GeminiAgent error: {e}] Falling back to simulation."

    def _simulate_reasoning(self, task: str) -> str:
        return f"[GeminiAgent/{self.model_name}] Task received: '{task[:80]}'. Executing via governed tools."


class LlamaAgent(AgentBase):
    """
    LLaMA 3.1 agent via Groq (free tier).
    Uses the groq SDK.
    Falls back to simulated reasoning if API key not set.
    """

    def __init__(self, session_id, principal_id, principal_type, private_key):
        super().__init__(
            session_id=session_id,
            principal_id=principal_id,
            principal_type=principal_type,
            model_name="llama-3.1-8b-instant",
            api_provider="groq",
            private_key=private_key,
        )
        self._api_key = os.environ.get("GROQ_API_KEY", "")

    def run_task(self, task_description: str) -> str:
        if not self._api_key:
            return self._simulate_reasoning(task_description)

        try:
            from groq import Groq
            client = Groq(api_key=self._api_key)
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system",  "content": self._build_system_prompt()},
                    {"role": "user",    "content": task_description},
                ],
                max_tokens=512,
            )
            raw = response.choices[0].message.content
            clean, _ = scan_response(
                session_id=self.session_id,
                tool="llm_output",
                raw_result=raw,
            )
            return clean
        except Exception as e:
            return f"[LlamaAgent error: {e}] Falling back to simulation."

    def _simulate_reasoning(self, task: str) -> str:
        return f"[LlamaAgent/{self.model_name}] Task received: '{task[:80]}'. Executing via governed tools."


class MistralAgent(AgentBase):
    """
    Mistral agent via Groq (free tier).
    Uses the groq SDK with mistral-saba model.
    Falls back to simulated reasoning if API key not set.
    """

    def __init__(self, session_id, principal_id, principal_type, private_key):
        super().__init__(
            session_id=session_id,
            principal_id=principal_id,
            principal_type=principal_type,
            model_name="mistral-saba-24b",
            api_provider="groq",
            private_key=private_key,
        )
        self._api_key = os.environ.get("GROQ_API_KEY", "")

    def run_task(self, task_description: str) -> str:
        if not self._api_key:
            return self._simulate_reasoning(task_description)

        try:
            from groq import Groq
            client = Groq(api_key=self._api_key)
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system",  "content": self._build_system_prompt()},
                    {"role": "user",    "content": task_description},
                ],
                max_tokens=512,
            )
            raw = response.choices[0].message.content
            clean, _ = scan_response(
                session_id=self.session_id,
                tool="llm_output",
                raw_result=raw,
            )
            return clean
        except Exception as e:
            return f"[MistralAgent error: {e}] Falling back to simulation."

    def _simulate_reasoning(self, task: str) -> str:
        return f"[MistralAgent/{self.model_name}] Task received: '{task[:80]}'. Executing via governed tools."


class GemmaAgent(AgentBase):
    """
    Gemma agent via Groq (free tier).
    Uses the groq SDK with gemma2-9b-it model.
    Falls back to simulated reasoning if API key not set.
    """

    def __init__(self, session_id, principal_id, principal_type, private_key):
        super().__init__(
            session_id=session_id,
            principal_id=principal_id,
            principal_type=principal_type,
            model_name="gemma2-9b-it",
            api_provider="groq",
            private_key=private_key,
        )
        self._api_key = os.environ.get("GROQ_API_KEY", "")

    def run_task(self, task_description: str) -> str:
        if not self._api_key:
            return self._simulate_reasoning(task_description)

        try:
            from groq import Groq
            client = Groq(api_key=self._api_key)
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system",  "content": self._build_system_prompt()},
                    {"role": "user",    "content": task_description},
                ],
                max_tokens=512,
            )
            raw = response.choices[0].message.content
            clean, _ = scan_response(
                session_id=self.session_id,
                tool="llm_output",
                raw_result=raw,
            )
            return clean
        except Exception as e:
            return f"[GemmaAgent error: {e}] Falling back to simulation."

    def _simulate_reasoning(self, task: str) -> str:
        return f"[GemmaAgent/{self.model_name}] Task received: '{task[:80]}'. Executing via governed tools."
