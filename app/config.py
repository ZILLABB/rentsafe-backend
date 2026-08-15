"""Application settings, loaded from environment (see .env.example)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder secrets that are fine locally but must never reach production.
_DEV_SECRETS = {"dev-secret-change-me", "change-me", "secret", ""}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "RentSafe Lagos API"
    environment: str = Field(default="development")
    debug: bool = Field(default=True)

    # SQLAlchemy async URL. Local dev defaults to zero-setup SQLite; production
    # uses Postgres+PostGIS, e.g. postgresql+asyncpg://user:pass@host:5432/rentsafe
    database_url: str = Field(default="sqlite+aiosqlite:///./rentsafe.db")

    # Echo every SQL statement (and its bound parameters, which include review
    # text) to the logs. Off by default — this is a debugging tool, not a
    # dev-mode default. Opt in with SQL_ECHO=true.
    sql_echo: bool = Field(default=False)

    redis_url: str = Field(default="redis://localhost:6379/0")

    # How many proxies sit in front of the app. X-Forwarded-For is appended to
    # by each hop, so only the last N entries are trustworthy — everything to
    # the left was supplied by the client and can be forged to defeat per-IP
    # limits. 0 means "no proxy": ignore the header entirely and use the socket
    # address. Set this to the real hop count when deploying behind a load
    # balancer; guessing high is worse than guessing low, because it lets a
    # caller choose their own identity.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=10)

    # Browser origins allowed to call the API. Comma-separated in the env.
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # Identity-system tuning (Section II).
    dedup_radius_m: float = Field(default=15.0)        # Step 5: ST_DWithin radius
    phash_radius_m: float = Field(default=100.0)       # Step 5: pHash comparison radius
    geocode_reinforce_m: float = Field(default=50.0)   # Step 4: pin/geocode agree
    geocode_divergence_m: float = Field(default=200.0)  # Step 4: flag for review

    # OTP throttling (Section XV). Requests are capped per phone and per client
    # IP; verification attempts are capped per issued code.
    otp_requests_per_hour: int = Field(default=3)
    otp_requests_per_day: int = Field(default=10)
    otp_resend_cooldown_s: int = Field(default=60)
    otp_max_attempts: int = Field(default=5)

    # Third-party keys (Section XIV) — optional until those features are wired.
    google_maps_api_key: str = Field(default="")
    mapbox_access_token: str = Field(default="")
    what3words_api_key: str = Field(default="")
    # Object storage for uploaded photos. Without a bucket, media lives on the
    # container filesystem and a redeploy destroys it — so this is required
    # outside development (see services.media.build_store).
    # Works with any S3-compatible provider: AWS, Cloudflare R2, DigitalOcean
    # Spaces, MinIO. Credentials come from the standard AWS_* environment
    # variables or an instance role.
    media_bucket: str = Field(default="")
    media_endpoint_url: str = Field(default="")  # non-AWS providers need this
    media_prefix: str = Field(default="media")
    # Web Push (VAPID). Optional by design: with no keys, push degrades to a
    # no-op and the UI says so, because unlike SMS nobody is locked out of the
    # product without it. Generate a pair with py-vapid; see .env.example.
    vapid_public_key: str = Field(default="")
    vapid_private_key: str = Field(default="")
    # Push services require a contact address for abuse reports.
    vapid_subject: str = Field(default="mailto:dev@rentsafe.local")
    termii_api_key: str = Field(default="")
    sentry_dsn: str = Field(default="")

    # Operational alerting for unattended jobs. Both optional.
    #
    # The heartbeat is pinged only when a backup *succeeds*, so an external
    # monitor alerts when the ping stops. That is the only way to detect the
    # job never running at all — a failure webhook cannot fire if cron is gone.
    backup_heartbeat_url: str = Field(default="")
    # Posted to on failure. Payload carries both `text` and `content`, so the
    # same body works with a Slack or a Discord incoming webhook unchanged.
    alert_webhook_url: str = Field(default="")

    # Signs JWTs. Rotating this invalidates live sessions — which is the point.
    jwt_secret: str = Field(default="dev-secret-change-me")

    # Salts the phone-number hashes. Deliberately SEPARATE from jwt_secret:
    # phone hashes are the only way back to a user row (we never store the
    # plaintext number), so this value is effectively immutable for the life of
    # the database. Rotating it orphans every account. Defaults to jwt_secret
    # only so existing local databases keep working.
    phone_hash_pepper: str = Field(default="")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        if not self.phone_hash_pepper:
            self.phone_hash_pepper = self.jwt_secret

        if self.environment.lower() in {"production", "prod", "staging"}:
            if self.debug:
                raise ValueError(
                    "DEBUG must be false outside development — it echoes OTP codes "
                    "in API responses and opens CORS."
                )
            if self.jwt_secret in _DEV_SECRETS:
                raise ValueError(
                    "JWT_SECRET is still the development placeholder. Set a real "
                    "secret (e.g. `openssl rand -hex 32`) before deploying."
                )
            if self.phone_hash_pepper in _DEV_SECRETS:
                raise ValueError(
                    "PHONE_HASH_PEPPER is still the development placeholder. Set a "
                    "real value before deploying — it cannot be changed later "
                    "without orphaning every user account."
                )
            if not self.cors_origins:
                raise ValueError("CORS_ORIGINS must list the frontend origin(s).")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
