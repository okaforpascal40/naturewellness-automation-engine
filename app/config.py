from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Supabase
    supabase_url: str = ""
    supabase_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""

    # External APIs
    open_targets_api_url: str = "https://api.platform.opentargets.org/api/v4/graphql"
    reactome_api_url: str = "https://reactome.org/ContentService"
    chembl_api_url: str = "https://www.ebi.ac.uk/chembl/api/data"
    usda_api_key: str = ""
    foodb_api_url: str = "https://foodb.ca"

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    # Accepts either a JSON array or a comma-separated string from the env var:
    # CORS_ORIGINS=https://app.com,https://other.com
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
