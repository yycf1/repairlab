"""统一模型客户端层（pi-ai 对应层）：OpenAI 兼容协议 + 原生 tool_calls。"""
from easycode.providers.openai_compat import ChatCompletionsClient, ProviderError

__all__ = ["ChatCompletionsClient", "ProviderError"]
