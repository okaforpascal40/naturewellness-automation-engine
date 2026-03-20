from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Supabase
    supabase_url: str = ""
    supabase_service_key: str = ""

    # External APIs
    open_targets_api_url: str = "https://api.platform.opentargets.org/api/v4/graphql"
    reactome_api_url: str = "https://reactome.org/ContentService"
    chembl_api_url: str = "https://www.ebi.ac.uk/chembl/api/data"
    usda_api_key: str = ""

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
