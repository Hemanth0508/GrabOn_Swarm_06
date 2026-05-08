from dotenv import load_dotenv
load_dotenv()

from providers.gemini_provider import GeminiProvider
from providers.groq_provider import LlamaProvider
from providers.openai_provider import OpenAIProvider


DUMMY_SESSION = "test-session"
DUMMY_PRINCIPAL = "test-principal"
DUMMY_TYPE = "AGENT"
DUMMY_KEY = None


def test_agent(agent_cls, label):
    print(f"\n=== Testing {label} ===")

    try:
        agent = agent_cls(
            session_id=DUMMY_SESSION,
            principal_id=DUMMY_PRINCIPAL,
            principal_type=DUMMY_TYPE,
            private_key=DUMMY_KEY,
        )

        result = agent.run_task(
            "Respond in one sentence: provider connection successful."
        )

        print(result[:300])

    except Exception as e:
        print(f"ERROR: {e}")


test_agent(GeminiProvider, "Gemini")
test_agent(LlamaProvider, "Groq Llama")
test_agent(OpenAIProvider, "OpenAI")