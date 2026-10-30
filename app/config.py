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


settings = Settings()
