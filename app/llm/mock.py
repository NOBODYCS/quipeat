import json
from dataclasses import dataclass

from .providers import ChatResult


@dataclass
class MockProvider:
    """Deterministic stand-in for tests/dev. Detects the requested schema by content."""
    name = "mock"

    def chat(self, model: str, system: str, user: str, *, json_schema: dict | None = None,
             temperature: float = 0.2, max_tokens: int = 4096) -> ChatResult:
        schema_str = json.dumps(json_schema or {})
        if "verdict" in schema_str:
            payload = {"verdict": "pass", "score": 0.95,
                       "notes": "Mock verification: answer is supported by the source chunk."}
        else:
            chunk = user[:80].replace("\n", " ")
            payload = {"questions": [
                {"prompt": f"[mock] According to the text, what is discussed here: {chunk}…?",
                 "options": ["The main idea", "An unrelated topic", "A footnote", "Nothing"],
                 "correct_index": 0,
                 "explanation": "The chunk states its main idea directly.",
                 "difficulty": "easy"},
                {"prompt": f"[mock] Which detail from the text supports the main idea: {chunk}…?",
                 "options": ["A supporting detail", "A contradiction", "A guess", "An opinion"],
                 "correct_index": 0,
                 "explanation": "The text provides supporting details for the main idea.",
                 "difficulty": "medium"},
            ]}
        return ChatResult(text=json.dumps(payload), model="mock-model",
                          input_tokens=len(user) // 4, output_tokens=50)
