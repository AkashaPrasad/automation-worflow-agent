"""Runtime configuration, read from environment (and `.env` in development)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


def _load_dotenv() -> None:
    # Minimal .env loader (no extra dependency). Real env vars win.
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            break


_load_dotenv()


class Settings(BaseModel):
    # System 2: planning, writing, reflection (Meta Model API, OpenAI-compatible)
    model_api_key: str = os.getenv("MODEL_API_KEY", "")
    model_base_url: str = os.getenv("MODEL_BASE_URL", "https://api.meta.ai/v1")
    model_name: str = os.getenv("MODEL_NAME", "muse-spark-1.3-contributor")
    # $ per 1M tokens, used for budget accounting
    model_price_in: float = float(os.getenv("MODEL_PRICE_IN", "0.10"))
    model_price_out: float = float(os.getenv("MODEL_PRICE_OUT", "0.20"))

    # System 1: calibrated judgments (TypeSafe Jev), Laya as open-weight fallback
    typesafe_api_key: str = os.getenv("TYPESAFE_API_KEY", "")
    jev_model: str = os.getenv("JEV_MODEL", "jev-latest")
    jev_price_in: float = float(os.getenv("JEV_PRICE_IN", "0.042"))
    laya_enabled: bool = os.getenv("LAYA_ENABLED", "auto") != "false"  # "auto": use if importable

    # Storage
    database_path: str = os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parents[1] / "adjutant.db"))

    # Web
    cors_origins: list[str] = [
        o.strip()
        for o in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,https://aiml.spacesdrive.cc",
        ).split(",")
        if o.strip()
    ]
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:5173")
    runs_per_hour_per_workspace: int = int(os.getenv("RUNS_PER_HOUR_PER_WORKSPACE", "20"))
    max_concurrent_runs: int = int(os.getenv("MAX_CONCURRENT_RUNS", "8"))

    # Live integrations (optional; the sandbox workspace is used when absent)
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    notion_token: str = os.getenv("NOTION_TOKEN", "")
    notion_parent_page_id: str = os.getenv("NOTION_PARENT_PAGE_ID", "")
    slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN", "")
    fireflies_api_key: str = os.getenv("FIREFLIES_API_KEY", "")
    mcp_servers: str = os.getenv("MCP_SERVERS", "")  # "name=https://host/mcp,other=https://..."

    # Test hook: when set, the Muse client returns scripted responses (unit tests only)
    fake_llm: bool = os.getenv("ADJUTANT_FAKE_LLM", "") == "1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
