from dataclasses import dataclass

import httpx


@dataclass
class ChatResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class ProviderError(Exception):
    pass


class BaseProvider:
    name: str = "base"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def chat(self, model: str, system: str, user: str, *, json_schema: dict | None = None,
             temperature: float = 0.2, max_tokens: int = 4096) -> ChatResult:
        raise NotImplementedError


class OpenRouterProvider(BaseProvider):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"

    def chat(self, model: str, system: str, user: str, *, json_schema: dict | None = None,
             temperature: float = 0.2, max_tokens: int = 4096) -> ChatResult:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": json_schema},
            }
        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ProviderError(f"OpenRouter {e.response.status_code}: {e.response.text[:300]}")
        except httpx.HTTPError as e:
            raise ProviderError(f"OpenRouter request failed: {e}")
        data = resp.json()
        choice = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return ChatResult(
            text=choice,
            model=data.get("model", model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


class MistralProvider(BaseProvider):
    name = "mistral"
    base_url = "https://api.mistral.ai/v1"

    def chat(self, model: str, system: str, user: str, *, json_schema: dict | None = None,
             temperature: float = 0.2, max_tokens: int = 4096) -> ChatResult:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ProviderError(f"Mistral {e.response.status_code}: {e.response.text[:300]}")
        except httpx.HTTPError as e:
            raise ProviderError(f"Mistral request failed: {e}")
        data = resp.json()
        choice = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return ChatResult(
            text=choice,
            model=data.get("model", model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


PROVIDERS = {p.name: p for p in (OpenRouterProvider, MistralProvider)}

DEFAULT_MODELS = {
    "openrouter": [
        "mistralai/mistral-large-latest",
        "anthropic/claude-sonnet-4",
        "openai/gpt-5",
        "google/gemini-2.5-pro",
        "qwen/qwen3-235b-a22b",
    ],
    "mistral": [
        "mistral-large-latest",
        "mistral-medium-latest",
        "mistral-small-latest",
        "ministral-8b-latest",
    ],
}


def make_provider(provider: str, api_key: str) -> BaseProvider:
    cls = PROVIDERS.get(provider)
    if cls is None:
        raise ProviderError(f"Unknown provider: {provider}")
    return cls(api_key)
