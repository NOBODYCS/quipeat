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


# Deck depth levels: questions per chunk, same-domain pages to fetch for URL
# sources, difficulty guidance injected into the generation prompt, and a hard
# cap on questions per deck so "thorough" can't bill unbounded tokens.
LEVELS = {
    "quick": {
        "questions_per_chunk": 1,
        "max_pages": 1,
        "max_questions": 30,
        "difficulty_hint": (
            "\n\nDIFFICULTY: Beginner. Ask straightforward fact-recall questions about the "
            "most important concepts a newcomer must know. Keep phrasing simple and direct."),
    },
    "standard": {
        "questions_per_chunk": 2,
        "max_pages": 15,
        "max_questions": 150,
        "difficulty_hint": (
            "\n\nDIFFICULTY: Intermediate. Mix fact-recall with questions that require "
            "understanding and applying the material (inference, cause/effect, how-it-works)."),
    },
    "thorough": {
        "questions_per_chunk": 4,
        "max_pages": 60,
        "max_questions": 400,
        "difficulty_hint": (
            "\n\nDIFFICULTY: Advanced and extensive. Cover the material exhaustively: facts, "
            "mechanics, configuration details, edge cases, and multi-concept synthesis that "
            "requires combining two or more ideas from the text. Prefer precise, specific "
            "questions over generic ones."),
    },
}
DEFAULT_LEVEL = "standard"


settings = Settings()
