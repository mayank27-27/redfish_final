"""
config.py
==========
Central configuration for the Redfish Fleet Monitor.

IMPORTANT: All settings are read from environment variables at startup.
Load your .env file BEFORE importing this module (done at the top of app.py).

Design
------
AppConfig is a plain object (not a dict-like Flask Config subclass) so that
all modules can access settings with either:
    config['REDFISH_MAX_RETRIES']      # dict-style (Flask app.config)
    config.REDFISH_MAX_RETRIES         # attribute-style (session.py, client.py)

The factory function build_app_config() reads every setting from
os.environ with sensible defaults, validates required values, and returns
an AppConfig instance. This is called once per process and the result
is stored as app.config['REDFISH_CONFIG'] so every module that receives
app.config can retrieve it.
"""
import logging
import os
from datetime import timedelta

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# AppConfig — dual attribute + dict access
# ─────────────────────────────────────────────────────────────────────────────

class AppConfig:
    """Configuration object with BOTH attribute (.KEY) and dict (['KEY']) access.

    This eliminates the mismatch between:
      - Flask's app.config (dict-like, only supports ['KEY'])
      - session.py / client.py (written to use config.KEY attribute style)
    """

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            object.__setattr__(self, k, v)

    # dict-style access so existing code using config['KEY'] keeps working
    def __getitem__(self, key):
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key, default=None):
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            return default

    def __contains__(self, key):
        return hasattr(self, key)

    def __repr__(self):
        safe_keys = {k: v for k, v in self.__dict__.items() if 'KEY' not in k and 'PASSWORD' not in k and 'SECRET' not in k}
        return f"AppConfig({safe_keys})"


def _bool(val: str, default: bool) -> bool:
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes")


def _int(val: str, default: int, name: str) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("Config: %s='%s' is not a valid integer, using default %d", name, val, default)
        return default


def _float(val: str, default: float, name: str) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        logger.warning("Config: %s='%s' is not a valid float, using default %.1f", name, val, default)
        return default


def build_app_config() -> AppConfig:
    """Read all settings from environment and return a validated AppConfig.

    Called once at startup. Logs a startup summary including all critical
    settings (without exposing secret values).
    """
    env = os.environ

    # ── Validate ENCRYPTION_KEY ────────────────────────────────────────────
    encryption_key = env.get("ENCRYPTION_KEY", "").strip()
    if encryption_key:
        _validate_fernet_key(encryption_key)
    else:
        from cryptography.fernet import Fernet
        encryption_key = Fernet.generate_key().decode()
        logger.warning(
            "════════════════════════════════════════════════════════════\n"
            "  ENCRYPTION_KEY is not set in your environment / .env file.\n"
            "  A TEMPORARY key has been generated for this process only.\n"
            "  ALL stored BMC passwords will be unreadable after restart.\n"
            "  Set ENCRYPTION_KEY in .env to a stable Fernet key.\n"
            "  Generate: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
            "════════════════════════════════════════════════════════════"
        )

    # ── Database ───────────────────────────────────────────────────────────
    db_url = env.get("DATABASE_URL", "postgresql://redfish:redfish@localhost:5432/redfishmonitor")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    # ── Email ticket alerts (SMTP) ──────────────────────────────────────────
    # Sends a ticket-style email for every newly-raised CRITICAL alert.
    # Disabled until SMTP_USERNAME + SMTP_PASSWORD are set in .env - Gmail
    # requires an App Password (2-Step Verification -> App Passwords), a
    # normal account password will be rejected.
    smtp_username = env.get("SMTP_USERNAME", "").strip()
    smtp_password = env.get("SMTP_PASSWORD", "").strip()
    smtp_from = env.get("SMTP_FROM", "").strip() or smtp_username

    # ── Build config object ────────────────────────────────────────────────
    cfg = AppConfig(
        # Flask
        SECRET_KEY=env.get("SECRET_KEY", "change-me-in-production"),
        DEBUG=_bool(env.get("DEBUG"), False),
        FLASK_ENV=env.get("FLASK_ENV", "development"),

        # Database
        SQLALCHEMY_DATABASE_URI=db_url,
        DATABASE_URL=db_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={
            "pool_pre_ping": True,
        } if db_url.startswith("sqlite") else {
            "pool_pre_ping": True,
            "pool_size": 10,
            "max_overflow": 20,
        },

        # Encryption
        ENCRYPTION_KEY=encryption_key,

        # Redfish HTTP client
        REDFISH_VERIFY_TLS=_bool(env.get("REDFISH_VERIFY_TLS"), False),
        REDFISH_HTTP_TIMEOUT=_float(env.get("REDFISH_HTTP_TIMEOUT"), 30.0, "REDFISH_HTTP_TIMEOUT"),
        REDFISH_MAX_RETRIES=_int(env.get("REDFISH_MAX_RETRIES"), 3, "REDFISH_MAX_RETRIES"),
        REDFISH_RETRY_BACKOFF_SECONDS=_float(env.get("REDFISH_RETRY_BACKOFF_SECONDS"), 2.0, "REDFISH_RETRY_BACKOFF_SECONDS"),

        # Session management
        REDFISH_SESSION_REFRESH_MARGIN=timedelta(minutes=5),

        # Polling
        DEFAULT_POLLING_INTERVAL_SECONDS=_int(env.get("DEFAULT_POLLING_INTERVAL_SECONDS"), 30, "DEFAULT_POLLING_INTERVAL_SECONDS"),
        MAX_CONCURRENT_POLLS=_int(env.get("MAX_CONCURRENT_POLLS"), 50, "MAX_CONCURRENT_POLLS"),
        INVENTORY_REFRESH_INTERVAL_SECONDS=_int(env.get("INVENTORY_REFRESH_INTERVAL_SECONDS"), 900, "INVENTORY_REFRESH_INTERVAL_SECONDS"),

        # Alerts
        FALLBACK_TEMPERATURE_CRITICAL_C=_float(env.get("FALLBACK_TEMPERATURE_CRITICAL_C"), 85.0, "FALLBACK_TEMPERATURE_CRITICAL_C"),

        # Email ticket alerts
        SMTP_HOST=env.get("SMTP_HOST", ""),
        SMTP_PORT=_int(env.get("SMTP_PORT"), 587, "SMTP_PORT"),
        SMTP_USERNAME=smtp_username,
        SMTP_PASSWORD=smtp_password,
        SMTP_FROM=smtp_from,
        ALERT_EMAIL_TO=env.get("ALERT_EMAIL_TO", ""),
        SMTP_ENABLED=bool(smtp_username and smtp_password),

        # History
        SENSOR_HISTORY_RETENTION_DAYS=_int(env.get("SENSOR_HISTORY_RETENTION_DAYS"), 30, "SENSOR_HISTORY_RETENTION_DAYS"),

        # EventService webhook
        PUBLIC_WEBHOOK_BASE_URL=env.get("PUBLIC_WEBHOOK_BASE_URL", ""),

        # SocketIO
        SOCKETIO_ASYNC_MODE=env.get("SOCKETIO_ASYNC_MODE", "eventlet"),
        SOCKETIO_MESSAGE_QUEUE=env.get("SOCKETIO_MESSAGE_QUEUE", "") or None,

        # CORS
        CORS_ALLOWED_ORIGINS=env.get("CORS_ALLOWED_ORIGINS", "*"),

        # Agent enrollment
        ENROLLMENT_KEY_HASH=env.get("ENROLLMENT_KEY_HASH", ""),
    )

    _log_startup_summary(cfg)
    return cfg


def _validate_fernet_key(key: str):
    """Validate that ENCRYPTION_KEY is a proper Fernet key.
    Raises ValueError with a clear message if it is not."""
    import base64
    key_bytes = key.encode() if isinstance(key, str) else key
    try:
        decoded = base64.urlsafe_b64decode(key_bytes)
    except Exception:
        raise ValueError(
            f"ENCRYPTION_KEY is not valid base64. Length={len(key)}. "
            f"A valid Fernet key is exactly 44 URL-safe base64 characters. "
            f"Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    if len(decoded) != 32:
        raise ValueError(
            f"ENCRYPTION_KEY decodes to {len(decoded)} bytes but Fernet requires exactly 32. "
            f"Key length={len(key)} chars. This usually means multiple keys were concatenated. "
            f"Use only ONE key. Generate: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        from cryptography.fernet import Fernet
        Fernet(key_bytes)
    except Exception as exc:
        raise ValueError(f"ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc


def _log_startup_summary(cfg: AppConfig):
    """Print a clear startup diagnostic so problems are visible immediately."""
    db = cfg.SQLALCHEMY_DATABASE_URI
    # Mask password in URI for log
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(db)
        safe_db = urlunparse(p._replace(netloc=f"{p.username}:***@{p.hostname}:{p.port}"))
    except Exception:
        safe_db = db
    logger.info(
        "\n"
        "═══════════════════════════════════════════════════════\n"
        "  Redfish Fleet Monitor — Startup Configuration\n"
        "═══════════════════════════════════════════════════════\n"
        "  Database:           %s\n"
        "  Encryption key:     %s\n"
        "  TLS verify:         %s\n"
        "  HTTP timeout:       %ss\n"
        "  Max retries:        %s\n"
        "  Poll interval:      %ss\n"
        "  Max concurrent:     %s servers\n"
        "  Inventory refresh:  %ss\n"
        "  SocketIO mode:      %s\n"
        "  Email alerts:       %s\n"
        "  Enrollment key:     %s\n"
        "═══════════════════════════════════════════════════════",
        safe_db,
        "SET (44 chars)" if cfg.ENCRYPTION_KEY else "NOT SET (temp key)",
        cfg.REDFISH_VERIFY_TLS,
        cfg.REDFISH_HTTP_TIMEOUT,
        cfg.REDFISH_MAX_RETRIES,
        cfg.DEFAULT_POLLING_INTERVAL_SECONDS,
        cfg.MAX_CONCURRENT_POLLS,
        cfg.INVENTORY_REFRESH_INTERVAL_SECONDS,
        cfg.SOCKETIO_ASYNC_MODE,
        f"ENABLED (critical alerts -> {cfg.ALERT_EMAIL_TO})" if cfg.SMTP_ENABLED
            else "DISABLED (set SMTP_USERNAME/SMTP_PASSWORD in .env)",
        "SET" if cfg.ENROLLMENT_KEY_HASH else "NOT SET (agent enrollment disabled)",
    )


# Keep a simple map for any code that still imports config_map
# (not used in the new flow but harmless)
config_map = {"development": build_app_config, "production": build_app_config, "default": build_app_config}