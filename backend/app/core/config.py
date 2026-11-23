"""Application configuration loading and validation."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from pydantic import BaseModel, Field

_ROOT_DIR = Path(__file__).resolve().parents[3]
_ROOT_ENV = _ROOT_DIR / ".env"

_IS_RUNNING_TESTS = (
    "unittest" in sys.modules
    or any("unittest" in str(arg).lower() or "pytest" in str(arg).lower() for arg in sys.argv)
    or os.environ.get("TESTING") == "1"
    or os.environ.get("RECALLRELAY_TESTING") == "1"
)

if _ROOT_ENV.exists() and (not _IS_RUNNING_TESTS or os.environ.get("RECALLRELAY_LIVE_SMOKE") == "1"):
    load_dotenv(dotenv_path=_ROOT_ENV, override=False)
elif _IS_RUNNING_TESTS and os.environ.get("RECALLRELAY_LIVE_SMOKE") != "1":
    for _k in (
        "BAND_SENDER_API_KEY",
        "BAND_RECEIVER_API_KEY",
        "BAND_RECEIVER_AGENT_ID",
        "LYZR_API_KEY",
        "LYZR_AGENT_ID",
    ):
        os.environ.pop(_k, None)

_DEFAULT_DB_PATH = str(_ROOT_DIR / "backend" / "data" / "recall_relay.db")


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_origins(value: str | None, default: List[str]) -> List[str]:
    if not value:
        return default
    origins = [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]
    return origins or default


def _parse_csv(value: str | None) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_transport(value: str | None) -> str:
    cleaned = (value or "internal_sqlite").strip().lower()
    if cleaned in {"internal_sqlite", "band"}:
        return cleaned
    return "internal_sqlite"


class Settings(BaseModel):
    """Typed runtime settings for the RecallRelay backend."""

    app_name: str = Field(default="RecallRelay API")
    app_version: str = Field(default="1.0.0")
    app_env: str = Field(default="development")
    demo_mode: bool = Field(default=True)
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000, ge=1, le=65535)
    database_path: str = Field(default=_DEFAULT_DB_PATH)
    admin_api_key: str | None = Field(default=None)
    agent_default_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    a2a_max_delegation_depth: int = Field(default=5, ge=1, le=20)
    a2a_transport: str = Field(default="internal_sqlite")
    session_ttl_minutes: int = Field(default=60, ge=5, le=1440)
    session_cookie_secure: bool = Field(default=False)
    external_http_timeout_seconds: float = Field(default=10.0, gt=0.5, le=30.0)
    external_max_retries: int = Field(default=2, ge=0, le=3)

    # Login throttling & explicit trusted reverse-proxy configuration
    login_throttle_max: int = Field(default=5, ge=1, le=100)
    login_throttle_window_seconds: int = Field(default=900, ge=30, le=86400)
    login_throttle_lockout_seconds: int = Field(default=900, ge=30, le=86400)
    trust_proxy_headers: bool = Field(default=False)
    trusted_proxy_ips: List[str] = Field(default_factory=list)

    # Optional openFDA API key (keyless public tier works without a key)
    openfda_api_key: str | None = Field(default=None)

    # Standardized BAND configuration (single canonical naming scheme)
    band_api_base_url: str = Field(default="https://app.band.ai")
    band_sender_api_key: str | None = Field(default=None)
    band_sender_agent_id: str | None = Field(default=None)
    band_sender_handle: str | None = Field(default=None)
    band_receiver_api_key: str | None = Field(default=None)
    band_receiver_agent_id: str | None = Field(default=None)
    band_receiver_handle: str | None = Field(default=None)
    band_room_id: str | None = Field(default=None)
    band_poll_attempts: int = Field(default=10, ge=1, le=60)
    band_poll_interval_seconds: float = Field(default=0.5, ge=0.0, le=10.0)

    # Lyzr v3 Agents API
    lyzr_api_base_url: str = Field(default="https://agent-prod.studio.lyzr.ai")
    lyzr_api_key: str | None = Field(default=None)
    lyzr_agent_id: str | None = Field(default=None)

    # Finch / AdaL / Agent37
    finch_mcp_url: str = Field(default="https://www.finchtech.ai/mcp")
    finch_api_key: str | None = Field(default=None)
    finch_cli_path: str | None = Field(default=None)

    adal_cli_path: str | None = Field(default=None)

    agent37_api_key: str | None = Field(default=None)

    # Explicit cross-origin (Vite dev) and single-origin (FastAPI/Docker/production) trusted origins
    cors_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )
    app_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )

    @property
    def band_api_key(self) -> str | None:
        return self.band_sender_api_key

    @property
    def band_agent_id(self) -> str | None:
        return self.band_sender_agent_id

    @property
    def login_rate_limit_max_failures(self) -> int:
        return self.login_throttle_max

    @property
    def login_rate_limit_window_seconds(self) -> int:
        return self.login_throttle_window_seconds

    @property
    def trusted_origins(self) -> List[str]:
        seen: set[str] = set()
        merged: List[str] = []
        for origin in [*self.cors_origins, *self.app_origins]:
            norm = origin.strip().rstrip("/")
            if norm and norm not in seen:
                seen.add(norm)
                merged.append(norm)
        return merged


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached validated settings loaded from environment variables."""
    app_env = os.getenv("APP_ENV", "development")
    is_prod = app_env.strip().lower() == "production"
    api_port = int(os.getenv("API_PORT", "8000"))
    default_app_origins = (
        []
        if is_prod
        else [f"http://localhost:{api_port}", f"http://127.0.0.1:{api_port}"]
    )
    throttle_max = int(
        os.getenv("LOGIN_THROTTLE_MAX")
        or os.getenv("LOGIN_RATE_LIMIT_MAX_FAILURES")
        or "5"
    )
    throttle_window = int(
        os.getenv("LOGIN_THROTTLE_WINDOW_SECONDS")
        or os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS")
        or "900"
    )
    throttle_lockout = int(
        os.getenv("LOGIN_THROTTLE_LOCKOUT_SECONDS") or str(throttle_window)
    )
    return Settings(
        app_name=os.getenv("APP_NAME", "RecallRelay API"),
        app_version=os.getenv("APP_VERSION", "1.0.0"),
        app_env=app_env,
        demo_mode=_parse_bool(os.getenv("DEMO_MODE"), default=True),
        api_host=os.getenv("API_HOST", "127.0.0.1"),
        api_port=api_port,
        database_path=os.getenv("DATABASE_PATH", _DEFAULT_DB_PATH),
        admin_api_key=_parse_optional(os.getenv("ADMIN_API_KEY")),
        agent_default_timeout_seconds=float(
            os.getenv("AGENT_DEFAULT_TIMEOUT_SECONDS", "5.0")
        ),
        a2a_max_delegation_depth=int(os.getenv("A2A_MAX_DELEGATION_DEPTH", "5")),
        a2a_transport=_parse_transport(os.getenv("A2A_TRANSPORT")),
        session_ttl_minutes=int(os.getenv("SESSION_TTL_MINUTES", "60")),
        session_cookie_secure=_parse_bool(
            os.getenv("SESSION_COOKIE_SECURE"), default=is_prod
        ),
        external_http_timeout_seconds=float(
            os.getenv("EXTERNAL_HTTP_TIMEOUT_SECONDS", "10.0")
        ),
        external_max_retries=int(os.getenv("EXTERNAL_MAX_RETRIES", "2")),
        login_throttle_max=throttle_max,
        login_throttle_window_seconds=throttle_window,
        login_throttle_lockout_seconds=throttle_lockout,
        trust_proxy_headers=_parse_bool(
            os.getenv("TRUST_PROXY_HEADERS"), default=False
        ),
        trusted_proxy_ips=_parse_csv(os.getenv("TRUSTED_PROXY_IPS")),
        openfda_api_key=_parse_optional(os.getenv("OPENFDA_API_KEY")),
        band_api_base_url=(
            _parse_optional(os.getenv("BAND_API_BASE_URL")) or "https://app.band.ai"
        ),
        band_sender_api_key=_parse_optional(os.getenv("BAND_SENDER_API_KEY")),
        band_sender_agent_id=_parse_optional(os.getenv("BAND_SENDER_AGENT_ID")),
        band_sender_handle=_parse_optional(os.getenv("BAND_SENDER_HANDLE")),
        band_receiver_api_key=_parse_optional(os.getenv("BAND_RECEIVER_API_KEY")),
        band_receiver_agent_id=_parse_optional(os.getenv("BAND_RECEIVER_AGENT_ID")),
        band_receiver_handle=_parse_optional(os.getenv("BAND_RECEIVER_HANDLE")),
        band_room_id=_parse_optional(os.getenv("BAND_ROOM_ID")),
        band_poll_attempts=int(os.getenv("BAND_POLL_ATTEMPTS", "10")),
        band_poll_interval_seconds=float(
            os.getenv("BAND_POLL_INTERVAL_SECONDS", "0.5")
        ),
        lyzr_api_base_url=os.getenv(
            "LYZR_API_BASE_URL", "https://agent-prod.studio.lyzr.ai"
        ).strip(),
        lyzr_api_key=_parse_optional(os.getenv("LYZR_API_KEY")),
        lyzr_agent_id=_parse_optional(os.getenv("LYZR_AGENT_ID")),
        finch_mcp_url=os.getenv(
            "FINCH_MCP_URL", "https://www.finchtech.ai/mcp"
        ).strip(),
        finch_api_key=_parse_optional(os.getenv("FINCH_API_KEY")),
        finch_cli_path=_parse_optional(os.getenv("FINCH_CLI_PATH")),
        adal_cli_path=_parse_optional(os.getenv("ADAL_CLI_PATH")),
        agent37_api_key=_parse_optional(os.getenv("AGENT37_API_KEY")),
        cors_origins=_parse_origins(
            os.getenv("CORS_ORIGINS"),
            default=["http://localhost:5173", "http://127.0.0.1:5173"],
        ),
        app_origins=_parse_origins(
            os.getenv("APP_ORIGINS"),
            default=default_app_origins,
        ),
    )
