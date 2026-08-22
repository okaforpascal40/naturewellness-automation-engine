from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": deployment environments inject variables this model does
    # not declare (Railway/Vercel internals, an operator's SERVICE_ROLE_KEY).
    # The default "forbid" turns any one of them into a validation error that
    # takes down every caller of get_settings() — Supabase, ChEMBL, the lot —
    # and surfaces as an empty result rather than an obvious config failure.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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

    # NCBI E-utilities (PubMed) — lifts the rate limit when set
    ncbi_api_key: str = ""

    # DisGeNET — supplements Open Targets with extra disease-gene associations.
    # The old open endpoint at www.disgenet.org/api was retired; the current
    # API requires a key from https://disgenet.com/. Unset = feature disabled.
    disgenet_api_key: str = ""
    disgenet_api_url: str = "https://api.disgenet.com/api/v1"

    # Plant.id / Kindwise crop identification (CamScan)
    plantid_api_key: str = ""
    plantid_api_url: str = "https://crop.kindwise.com/api/v1/identification"

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
