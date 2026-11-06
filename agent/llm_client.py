"""LLM client that speaks the OpenAI tool-calling protocol.

Two providers are supported:

* ``openrouter`` - the OpenAI SDK pointed at OpenRouter's ``/api/v1`` endpoint;
  ``model`` is the OpenRouter model id (e.g. ``anthropic/claude-sonnet-4.5``).
* ``azure`` - Microsoft's ``azure-ai-inference`` client pointed at an Azure
  OpenAI deployment URL. No ``api_version`` is required because this SDK
  bakes the protocol version in.

Both providers return an assistant message with ``.content`` and
``.tool_calls``; the calling loop in :mod:`agent.agent_loop` does not need
to know which provider it is talking to.
"""

from __future__ import annotations

from openai import OpenAI
from openai.types.chat import ChatCompletionMessage


class LLMClient:
    """Provider-agnostic chat client."""

    def __init__(
        self,
        provider: str,
        *,
        api_key: str,
        # OpenRouter-only
        model: str | None = None,
        base_url: str | None = None,
        # Azure-only
        azure_endpoint: str | None = None,
        azure_deployment: str | None = None,
    ):
        provider = (provider or "openrouter").strip().lower()
        self._provider = provider

        if provider == "azure":
            if not (azure_endpoint and azure_deployment and api_key):
                raise ValueError(
                    "Azure provider requires api_key, azure_endpoint, and "
                    "azure_deployment."
                )
            # Lazy import so the OpenRouter-only deployment doesn't pay the
            # azure-ai-inference import cost.
            from azure.ai.inference import ChatCompletionsClient
            from azure.core.credentials import AzureKeyCredential

            # Azure OpenAI endpoint format for azure-ai-inference: append the
            # deployment path so the SDK targets that specific model.
            endpoint = azure_endpoint.rstrip("/") + f"/openai/deployments/{azure_deployment}"
            self._client = ChatCompletionsClient(
                endpoint=endpoint,
                credential=AzureKeyCredential(api_key),
            )
            self._model = azure_deployment

        elif provider == "openrouter":
            if not (model and base_url and api_key):
                raise ValueError(
                    "OpenRouter provider requires api_key, model, and base_url."
                )
            self._client = OpenAI(api_key=api_key, base_url=base_url)
            self._model = model

        else:
            raise ValueError(f"Unsupported LLM provider: {provider!r}")

    @property
    def provider(self) -> str:
        return self._provider

    def chat(self, messages: list[dict], tools: list[dict]) -> ChatCompletionMessage:
        """Send one chat completion request and return the assistant message.

        The caller inspects ``.content`` and ``.tool_calls`` to decide what
        happens next (final answer vs. another tool round-trip).
        """
        if self._provider == "azure":
            return self._chat_azure(messages, tools)
        return self._chat_openai(messages, tools)

    # ------------------------------------------------------------------ #
    def _chat_openai(self, messages, tools):
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        if not getattr(response, "choices", None):
            err = getattr(response, "error", None)
            raise RuntimeError(
                f"LLM returned no choices (provider error or rate limit): {err!r}"
            )
        return response.choices[0].message

    # ------------------------------------------------------------------ #
    def _chat_azure(self, messages, tools):
        # ``azure-ai-inference`` accepts raw dict messages + raw dict tools in
        # OpenAI format. The response message exposes ``.content`` and
        # ``.tool_calls`` with the same field names the agent loop expects, so
        # the only adaptation needed is unwrapping ``choices[0].message``.
        response = self._client.complete(
            messages=messages,
            tools=tools,
            tool_choice="auto",
            model=self._model,
        )
        if not getattr(response, "choices", None):
            raise RuntimeError("Azure LLM returned no choices.")
        return response.choices[0].message
