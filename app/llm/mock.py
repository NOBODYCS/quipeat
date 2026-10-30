import json
from dataclasses import dataclass

from .providers import ChatResult


def _rep(text: str, n: int = 30) -> str:
    return (text + " ") * n


@dataclass
class MockProvider:
    """Deterministic stand-in for tests/dev. Detects the requested schema by content."""
    name = "mock"

    def chat(self, model: str, system: str, user: str, *, json_schema: dict | None = None,
             temperature: float = 0.2, max_tokens: int = 4096) -> ChatResult:
        schema_str = json.dumps(json_schema or {})
        if "sections" in schema_str and "heading" in schema_str:
            # research-document call for topic mode: deterministic multi-section doc
            import re
            m = re.search(r"TOPIC: (.+)", user)
            topic = m.group(1).strip() if m else "the topic"
            payload = {"sections": [
                {"heading": f"Overview of {topic}",
                 "body": _rep(f"{topic} is a domain with core concepts, definitions, and "
                              f"relationships that learners must understand before applying them.")},
                {"heading": f"Key facts about {topic}",
                 "body": _rep(f"The most important facts about {topic} include its main "
                              f"definitions, standard practitioner terminology, and the "
                              f"foundational principles everything else builds on.")},
                {"heading": f"How {topic} works",
                 "body": _rep(f"At a high level, {topic} works through a small number of "
                              f"mechanisms: inputs are processed by well-defined rules into "
                              f"predictable outputs that can be reasoned about step by step.")},
                {"heading": f"Common misconceptions about {topic}",
                 "body": _rep(f"A frequent misconception is that {topic} is simpler than it is; "
                              f"it has edge cases and interactions beginners often miss.")},
                {"heading": f"Applying {topic}",
                 "body": _rep(f"To apply {topic} effectively, start with core examples, check "
                              f"understanding against realistic scenarios, and watch the "
                              f"failure modes practitioners document.")},
            ]}
        elif "is_learning_material" in schema_str:
            import re
            n_excerpts = len(re.findall(r"^--- Excerpt \d+ ---", user, re.M))
            n = n_excerpts if n_excerpts else 1
            payload = {"verdicts": [
                {"index": i, "is_learning_material": True,
                 "reason": "Mock triage: assume learning material."}
                for i in range(n)]}
        elif "verdict" in schema_str:
            import re
            n_questions = len(re.findall(r"^Question \d+:", user, re.M))
            n = n_questions if n_questions else 1
            payload = {"results": [
                {"index": i, "verdict": "pass", "score": 0.95,
                 "notes": "Mock verification: answer is supported by the source chunk."}
                for i in range(n)]}
        else:
            chunk = user[:80].replace("\n", " ")
            payload = {"questions": [
                {"prompt": f"[mock] What main idea is discussed here: {chunk}…?",
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
