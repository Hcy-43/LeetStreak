from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader so the app has no python-dotenv dependency."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    secret_key: str
    database_url: str
    base_url: str
    github_client_id: str
    github_client_secret: str
    github_token: str
    google_client_id: str
    google_client_secret: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_starttls: bool
    resend_api_key: str
    mail_from: str
    allow_registration: bool
    sync_ttl_seconds: int
    cron_token: str

    @property
    def github_oauth_enabled(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def mail_backend(self) -> str:
        """SMTP wins when both are set; 'console' means nobody receives anything."""
        if self.smtp_host:
            return "smtp"
        if self.resend_api_key:
            return "resend"
        return "console"

    @property
    def email_delivery_configured(self) -> bool:
        return self.mail_backend != "console"

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.base_url}/auth/google/callback"

    @property
    def github_api_enabled(self) -> bool:
        return bool(self.github_token)

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.base_url}/auth/github/callback"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(ROOT_DIR / ".env")

    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()

    # Hosts hand this out under either name; Render calls it DATABASE_URL.
    database_url = (
        os.environ.get("DATABASE_URL", "").strip()
        or "postgresql:///leetstreak_dev"
    )
    secret = os.environ.get("SECRET_KEY", "").strip()
    if not secret or secret == "change-me":
        # Ephemeral key: the app still runs, but sessions die on restart. Loud
        # enough in the logs that nobody ships this by accident.
        secret = secrets.token_hex(32)
        os.environ.setdefault("_LEETSTREAK_EPHEMERAL_SECRET", "1")

    return Settings(
        secret_key=secret,
        database_url=database_url,
        base_url=os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/"),
        github_client_id=client_id,
        github_client_secret=os.environ.get("GITHUB_CLIENT_SECRET", "").strip(),
        github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        smtp_host=os.environ.get("SMTP_HOST", "").strip(),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_username=os.environ.get("SMTP_USERNAME", "").strip(),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_starttls=_flag("SMTP_STARTTLS", default=True),
        resend_api_key=os.environ.get("RESEND_API_KEY", "").strip(),
        mail_from=os.environ.get("MAIL_FROM", "LeetStreak <no-reply@localhost>").strip(),
        allow_registration=_flag("ALLOW_REGISTRATION", default=True),
        sync_ttl_seconds=int(os.environ.get("SYNC_TTL_SECONDS", "900")),
        cron_token=os.environ.get("CRON_TOKEN", "").strip(),
    )


def using_ephemeral_secret() -> bool:
    return os.environ.get("_LEETSTREAK_EPHEMERAL_SECRET") == "1"
