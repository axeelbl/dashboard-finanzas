import os
from datetime import timedelta
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def load_configuration(app) -> None:
    _load_env_file()

    database_path = Path(os.getenv("DATABASE_PATH", BASE_DIR / "dashboard.db")).resolve()
    uploads_dir = Path(os.getenv("UPLOADS_DIR", BASE_DIR / "uploads")).resolve()
    backups_dir = Path(os.getenv("BACKUPS_DIR", BASE_DIR / "backups")).resolve()
    logs_dir = Path(os.getenv("LOGS_DIR", BASE_DIR / "logs")).resolve()

    uploads_dir.mkdir(parents=True, exist_ok=True)
    backups_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "change-this-secret-before-production"),
        DATABASE_PATH=str(database_path),
        UPLOADS_DIR=str(uploads_dir),
        BACKUPS_DIR=str(backups_dir),
        LOGS_DIR=str(logs_dir),
        APP_HOST=os.getenv("APP_HOST", "127.0.0.1"),
        APP_PORT=_env_int("APP_PORT", 5000),
        DEBUG=_env_bool("DEBUG", True),
        MAX_CONTENT_LENGTH=_env_int("MAX_CONTENT_LENGTH_MB", 12) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=_env_bool("SESSION_COOKIE_SECURE", False),
        SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=_env_int("SESSION_LIFETIME_HOURS", 12)),
        DEFAULT_CURRENCY=os.getenv("DEFAULT_CURRENCY", "EUR"),
        DEFAULT_ADMIN_USERNAME=os.getenv("ADMIN_USERNAME", "admin"),
        DEFAULT_ADMIN_PASSWORD_HASH=os.getenv("ADMIN_PASSWORD_HASH", "").strip(),
        DEFAULT_ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", "").strip(),
        APP_TITLE=os.getenv("APP_TITLE", "Dashboard de Finanzas"),
        ENABLE_MARKET_PRICE_REFRESH=_env_bool("ENABLE_MARKET_PRICE_REFRESH", False),
        COINGECKO_API_BASE_URL=os.getenv("COINGECKO_API_BASE_URL", "https://api.coingecko.com/api/v3").rstrip("/"),
        COINGECKO_API_KEY=os.getenv("COINGECKO_API_KEY", "").strip(),
        CRYPTO_PRICE_REFRESH_MINUTES=_env_int("CRYPTO_PRICE_REFRESH_MINUTES", 30),
        MARKET_PRICE_REFRESH_MINUTES=_env_int("MARKET_PRICE_REFRESH_MINUTES", _env_int("CRYPTO_PRICE_REFRESH_MINUTES", 30)),
        EMAIL_ENABLED=_env_bool("EMAIL_ENABLED", False),
        EMAIL_IMAP_HOST=os.getenv("EMAIL_IMAP_HOST", "").strip(),
        EMAIL_IMAP_PORT=_env_int("EMAIL_IMAP_PORT", 993),
        EMAIL_USERNAME=os.getenv("EMAIL_USERNAME", "").strip(),
        EMAIL_PASSWORD=os.getenv("EMAIL_PASSWORD", "").strip(),
        EMAIL_FOLDER=os.getenv("EMAIL_FOLDER", "INBOX").strip() or "INBOX",
        EMAIL_PROCESSED_FOLDER=os.getenv("EMAIL_PROCESSED_FOLDER", "Processed").strip() or "Processed",
        EMAIL_ERROR_FOLDER=os.getenv("EMAIL_ERROR_FOLDER", "Error").strip() or "Error",
        EMAIL_POLL_INTERVAL=_env_int("EMAIL_POLL_INTERVAL", 300),
        EMAIL_IMAP_TIMEOUT=_env_int("EMAIL_IMAP_TIMEOUT", 60),
        SQLITE_TIMEOUT_SECONDS=max(_env_int("SQLITE_TIMEOUT_SECONDS", 30), 1),
    )
