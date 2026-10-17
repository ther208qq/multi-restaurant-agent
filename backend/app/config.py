"""Application Configuration Settings."""

from pathlib import Path
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# Database created by scripts/import_public_sales_dataset.py. It carries the
# evaluation accounts (eval_admin / eval_staff) instead of the development seed.
EVAL_DATABASE_NAME = "restaurant_ai_eval"

# ``env_file=".env"`` resolves against the process working directory. PyCharm
# and ``python backend/run_eval.py`` launched with ``backend/`` as the working
# directory would then silently fall back to the defaults (Ollama on
# localhost:11434), which surfaces as “模型不可用” in the diagnosis panel.
# Anchor it to the repository root instead, the same way the test bootstrap does.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgrespassword@localhost:5432/restaurant_ai"

    # LLM provider seam. "ollama" = local/tunnelled Ollama server;
    # "openai" = any OpenAI-compatible /chat/completions endpoint (OpenAI,
    # DeepSeek, Qwen/DashScope, Moonshot, Groq, OpenRouter, vLLM, LM Studio...).
    LLM_PROVIDER: str = "ollama"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "deepseek-r1:8b"
    # Ollama's default context window is small; the diagnosis agent needs room
    # for a few raw tool results plus the investigation memos of earlier turns.
    OLLAMA_NUM_CTX: int = 8192
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-4o-mini"

    # Time-of-day / weekday splits in the business profile (Task61) are rendered
    # in this zone. Order timestamps stay UTC instants and the existing daily
    # aggregates stay UTC days; only the new profile dimensions use this zone.
    BUSINESS_TIMEZONE: str = "Asia/Shanghai"

    # Budget for the messages handed to the model (Task54). The system prompt
    # and the model's response need headroom inside OLLAMA_NUM_CTX.
    DIAGNOSIS_CONTEXT_TOKEN_BUDGET: int = 6000
    DIAGNOSIS_CONTEXT_CHAR_BUDGET: int = 24000
    # Task59: a slow probe is skipped after this many seconds; the turn keeps
    # what it already found. The turn deadline covers model calls and retries
    # and drops to the deterministic diagnosis when exceeded.
    DIAGNOSIS_TOOL_TIMEOUT_SECONDS: float = 10.0
    DIAGNOSIS_TURN_TIMEOUT_SECONDS: float = 90.0

    JWT_SECRET: str = "restaurant_ai_super_secret_jwt_key_change_in_production_32char_min"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ENVIRONMENT: str = "development"
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173"

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def dataset(self) -> str:
        """Which seeded account set the configured database belongs to."""
        try:
            database = make_url(self.DATABASE_URL).database
        except Exception:
            return "development"
        return "evaluation" if database == EVAL_DATABASE_NAME else "development"

    @property
    def sync_database_url(self) -> str:
        """Return synchronous connection string for psycopg3 / PostgresSaver."""
        url = self.DATABASE_URL
        if "+asyncpg" in url:
            url = url.replace("+asyncpg", "")
        if "@localhost:" in url:
            url = url.replace("@localhost:", "@127.0.0.1:")
        return url


settings = Settings()
