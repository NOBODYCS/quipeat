import json
import re
from typing import Type

from pydantic import BaseModel, ValidationError

from .providers import BaseProvider, ChatResult, ProviderError

MAX_RETRIES = 3


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


def structured_call(provider: BaseProvider, model: str, system: str, user: str,
                    schema_cls: Type[BaseModel], *, temperature: float = 0.2,
                    max_tokens: int = 4096) -> tuple[BaseModel, ChatResult]:
    """Call the model and parse output into a pydantic model, retrying with the
    validation error appended so even weaker models converge on the contract."""
    json_schema = schema_cls.model_json_schema()
    last_error = ""
    for attempt in range(MAX_RETRIES):
        prompt = user if not last_error else (
            f"{user}\n\nYour previous response failed validation: {last_error}\n"
            "Return ONLY valid JSON matching the required schema."
        )
        result = provider.chat(model, system, prompt, json_schema=json_schema,
                               temperature=temperature, max_tokens=max_tokens)
        try:
            parsed = schema_cls.model_validate(json.loads(_extract_json(result.text)))
            return parsed, result
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = str(e)[:1500]
    raise ProviderError(f"Model output failed validation after {MAX_RETRIES} attempts: {last_error}")
