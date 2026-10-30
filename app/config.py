from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    secret_key: str = "dev-insecure-change-me"
    master_key: str = ""
    database_path: str = "data/quipeat.db"
    upload_dir: str = "data/uploads"
    host: str = "0.0.0.0"
    port: int = 8000
    passkeys_enabled: bool = False
    rp_id: str = "localhost"
    rp_name: str = "Quipeat"
    llm_mock: bool = False
    max_upload_mb: int = 25
    # Server-side key for the built-in Mistral provider (admin/privileged only).
    mistral_api_key: str = ""
    builtin_model: str = "mistral-large-latest"
    # Email OR display name of the user who should be admin (matched case-insensitively).
    admin_ident: str = ""
    # Upper bound on pages fetched when crawling a site source (safety budget).
    crawl_max_pages: int = 60


# Deck depth levels. Size is defined by a TARGET QUESTION COUNT (user intent,
# predictable cost), not raw per-chunk multipliers. For each level:
#   target_questions  - the deck aims for roughly this many questions
#   questions_per_chunk - questions drafted per (sampled) chunk
#   max_pages         - same-domain pages fetched for URL sources
#   difficulty_hint    - extra instruction appended to the generation prompt
# If the source has more chunks than needed for the target, chunks are evenly
# sampled across the whole text so coverage stays broad at a bounded cost.
LEVELS = {
    "quick": {
        "target_questions": 50,
        "questions_per_chunk": 1,
        "max_pages": 4,
        "difficulty_hint": (
            "\n\nDIFFICULTY: Beginner. Ask straightforward fact-recall questions about the "
            "most important concepts a newcomer must know. Keep phrasing simple and direct."),
    },
    "standard": {
        "target_questions": 100,
        "questions_per_chunk": 2,
        "max_pages": 8,
        "difficulty_hint": (
            "\n\nDIFFICULTY: Intermediate. Mix fact-recall with questions that require "
            "understanding and applying the material (inference, cause/effect, how-it-works)."),
    },
    "thorough": {
        "target_questions": 250,
        "questions_per_chunk": 3,
        "max_pages": 20,
        "difficulty_hint": (
            "\n\nDIFFICULTY: Advanced and extensive. Cover the material in depth: facts, "
            "mechanics, configuration details, edge cases, and multi-concept synthesis that "
            "requires combining two or more ideas from the text. Prefer precise, specific "
            "questions over generic ones."),
    },
}
DEFAULT_LEVEL = "standard"


settings = Settings()
