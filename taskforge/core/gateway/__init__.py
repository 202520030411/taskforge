"""Model gateway (DESIGN.md section 6).

The only path to paid models: idempotent calls, a circuit breaker and a shared
rate limit per provider, the budget guard, quick retries, the cost ledger and the
prompt registry. P3 adds the Anthropic and OpenRouter adapters.
"""
from .fake import FakeProvider, tool_reply, turn_number
from .gateway import CircuitBreaker, Gateway, LLMCall, RateLimit
from .state import GatewayState, MemoryGatewayState, PgGatewayState
from .prompts import Prompt, PromptRegistry
from .types import (BudgetGuard, ModelRequest, ModelResponse, Provider, ProviderError, ToolCall, ToolSpec,
                    Usage)

__all__ = ["BudgetGuard", "CircuitBreaker", "FakeProvider", "Gateway", "GatewayState", "LLMCall",
           "MemoryGatewayState", "PgGatewayState", "RateLimit", "ModelRequest", "ModelResponse", "Prompt",
           "PromptRegistry", "Provider", "ProviderError", "ToolCall", "ToolSpec", "Usage", "tool_reply",
           "turn_number"]
