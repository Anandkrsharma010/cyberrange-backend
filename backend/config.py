"""
backend/config.py (updated)

Changes vs previous:
- Role constants added as single source of truth.
  Import these everywhere instead of hardcoding role strings.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from urllib.parse import urlparse

# ── Role constants ────────────────────────────────────────────────────────────
ROLE_SYS_ADMIN    = "sys_admin"
ROLE_COURSE_ADMIN = "course_admin"
ROLE_PARTICIPANT  = "participant"

ALL_ROLES = {ROLE_SYS_ADMIN, ROLE_COURSE_ADMIN, ROLE_PARTICIPANT}

# ── Guardrail defaults ────────────────────────────────────────────────────────
GUARDRAIL_DEFAULT_MAX_CONCURRENT  = 10
GUARDRAIL_DEFAULT_MAX_DURATION_HOURS = 4


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="backend/.env",
        extra="ignore",
        case_sensitive=False,
    )

    DATABASE_URL: str
    MIGRATION_DATABASE_URL: str
    JWT_SECRET: str
    GOOGLE_CLIENT_ID: str

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    JWT_ALGO: str = "HS256"
    JWT_ISSUER: str = "cyberrange"
    JWT_AUDIENCE: str = "cyberrange-users"
    ALLOWED_SSO_PROVIDERS: list[str] = ["google"]

    HEADSCALE_API_KEY: str
    HEADSCALE_API_URL: str
    HEADSCALE_LOGIN_SERVER: str = ""
    AWS_REGION: str = "ap-south-1"

    CORS_ALLOWED_ORIGINS: str = ""

    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_DEPLOY: str = "5/minute"
    RATE_LIMIT_TAILNET: str = "10/minute"
    RATE_LIMIT_BILLING: str = "10/minute"

    RAZORPAY_KEY_ID: str = "rzp_test_RGNVFEMmCNIdRM"
    RAZORPAY_KEY_SECRET: str = "dummy_secret"
    RAZORPAY_WEBHOOK_SECRET: str = "dummy_webhook_secret"

    TRUSTED_PROXY_IPS: list[str] = []
    REDIS_URL: str = "redis://localhost:6379/0"
    ENABLE_DOCS: bool = False

    # Workshop invites (Phase B) — links in email point here; learners complete accept after login.
    FRONTEND_PUBLIC_URL: str = "http://localhost:3000"
    WORKSHOP_INVITE_EXPIRE_DAYS: int = 14
    # log: print invite URL to application logs; smtp: send via SMTP; none: create invite without sending
    EMAIL_BACKEND: str = "log"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_USE_TLS: bool = True

    @field_validator("JWT_SECRET")
    @classmethod
    def jwt_secret_strength(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                "JWT_SECRET must be at least 32 characters. "
                "Generate one with: openssl rand -hex 32"
            )
        return v

    @field_validator("DATABASE_URL", "MIGRATION_DATABASE_URL")
    @classmethod
    def convert_postgresql_scheme(cls, v: str) -> str:
        if v and v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v


    def resolved_headscale_login_server(self) -> str:
        if self.HEADSCALE_LOGIN_SERVER and self.HEADSCALE_LOGIN_SERVER.strip():
            return self.HEADSCALE_LOGIN_SERVER.strip().rstrip("/")
        api_url = (self.HEADSCALE_API_URL or "").strip()
        if not api_url:
            return ""
        parsed = urlparse(api_url)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc
        if not netloc and parsed.path and "://" not in api_url:
            netloc = parsed.path
        if not netloc:
            return api_url.rstrip("/")
        return f"{scheme}://{netloc}".rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()