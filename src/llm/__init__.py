"""LLM Layer — multi-model router with provider abstraction.

Providers
---------
- OpenAIProvider     : OpenAI and Azure OpenAI (chat, embeddings, tools)
- AnthropicProvider  : Anthropic Claude and Amazon Bedrock (chat, tools)
- GoogleProvider     : Google Gemini / Vertex AI (chat, embeddings)

Router
------
- MultiModelRouter   : Routes by task type, manages fallbacks, tracks costs
"""

from src.llm.router import MultiModelRouter

__all__ = ["MultiModelRouter"]
