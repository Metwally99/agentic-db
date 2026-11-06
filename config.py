"""Application configuration loaded from environment / .env file."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "dummy_key")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")


def validate_llm_config() -> None:
    """Verify the configured LLM provider has all required credentials."""
    if LLM_PROVIDER == "azure":
        missing = [
            name for name, value in (
                ("AZURE_OPENAI_API_KEY", AZURE_OPENAI_API_KEY),
                ("AZURE_OPENAI_ENDPOINT", AZURE_OPENAI_ENDPOINT),
                ("AZURE_OPENAI_DEPLOYMENT", AZURE_OPENAI_DEPLOYMENT),
            ) if not value
        ]
        if missing:
            raise ValueError(
                "Missing required Azure OpenAI settings: " + ", ".join(missing)
            )
    elif LLM_PROVIDER == "openrouter":
        if not OPENROUTER_API_KEY:
            raise ValueError(
                "OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter."
            )
    else:
        raise ValueError(
            f"Unsupported LLM_PROVIDER: {LLM_PROVIDER!r}. "
            "Set LLM_PROVIDER=openrouter or LLM_PROVIDER=azure."
        )

DEFAULT_SQLITE_PATH = "mock_database/mock.db"
DB_PATH = os.getenv("DB_PATH", DEFAULT_SQLITE_PATH)


def _default_db_type() -> str:
    if os.getenv("DB_TYPE"):
        return os.getenv("DB_TYPE", "mysql").strip().lower()
    if os.getenv("DB_HOST") or os.getenv("DB_NAME"):
        return "mysql"
    return "sqlite"


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DB_TYPE = _default_db_type()
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_SSL_MODE = os.getenv("DB_SSL_MODE", "required").strip().lower()
DB_CA_CERT = os.getenv("DB_CA_CERT", "")
DB_CLIENT_CERT = os.getenv("DB_CLIENT_CERT", "")
DB_CLIENT_KEY = os.getenv("DB_CLIENT_KEY", "")
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))
DB_READ_TIMEOUT = int(os.getenv("DB_READ_TIMEOUT", "30"))
DB_WRITE_TIMEOUT = int(os.getenv("DB_WRITE_TIMEOUT", "30"))
DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "3"))
DB_CONNECT_RETRY_DELAY = float(os.getenv("DB_CONNECT_RETRY_DELAY", "1.0"))

DB_ALLOW_DDL = _get_bool("DB_ALLOW_DDL", True)
DB_BLOCK_DANGEROUS_SQL = _get_bool("DB_BLOCK_DANGEROUS_SQL", True)
DB_REQUIRE_WHERE_FOR_UPDATE_DELETE = _get_bool(
    "DB_REQUIRE_WHERE_FOR_UPDATE_DELETE", True
)

MAX_AGENT_ITERATIONS = 10


def get_database_settings() -> dict:
    """Return the configured database settings for backend initialization."""
    settings = {
        "type": DB_TYPE,
        "allow_ddl": DB_ALLOW_DDL,
        "block_dangerous_sql": DB_BLOCK_DANGEROUS_SQL,
        "require_where_for_update_delete": DB_REQUIRE_WHERE_FOR_UPDATE_DELETE,
    }
    if DB_TYPE == "sqlite":
        settings["path"] = DB_PATH
        return settings

    settings.update(
        {
            "host": DB_HOST,
            "port": DB_PORT,
            "name": DB_NAME,
            "user": DB_USER,
            "password": DB_PASSWORD,
            "ssl_mode": DB_SSL_MODE,
            "ca_cert": DB_CA_CERT,
            "client_cert": DB_CLIENT_CERT,
            "client_key": DB_CLIENT_KEY,
            "connect_timeout": DB_CONNECT_TIMEOUT,
            "read_timeout": DB_READ_TIMEOUT,
            "write_timeout": DB_WRITE_TIMEOUT,
            "connect_retries": DB_CONNECT_RETRIES,
            "connect_retry_delay": DB_CONNECT_RETRY_DELAY,
        }
    )
    return settings


def validate_database_config() -> None:
    """Validate the configured database settings before startup."""
    if DB_TYPE not in {"sqlite", "mysql"}:
        raise ValueError("DB_TYPE must be either 'sqlite' or 'mysql'.")

    if DB_TYPE == "sqlite":
        if not DB_PATH:
            raise ValueError("DB_PATH is required when DB_TYPE=sqlite.")
        return

    required = {
        "DB_HOST": DB_HOST,
        "DB_NAME": DB_NAME,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(
            "Missing required MySQL settings: " + ", ".join(sorted(missing))
        )

    allowed_ssl_modes = {"disabled", "required", "verify_ca", "verify_identity"}
    if DB_SSL_MODE not in allowed_ssl_modes:
        raise ValueError(
            "DB_SSL_MODE must be one of: disabled, required, verify_ca, verify_identity."
        )

    if DB_SSL_MODE in {"verify_ca", "verify_identity"} and not DB_CA_CERT:
        raise ValueError(
            "DB_CA_CERT is required when DB_SSL_MODE is verify_ca or verify_identity."
        )

    numeric_settings = {
        "DB_CONNECT_TIMEOUT": DB_CONNECT_TIMEOUT,
        "DB_READ_TIMEOUT": DB_READ_TIMEOUT,
        "DB_WRITE_TIMEOUT": DB_WRITE_TIMEOUT,
        "DB_CONNECT_RETRIES": DB_CONNECT_RETRIES,
    }
    invalid = [name for name, value in numeric_settings.items() if value <= 0]
    if invalid:
        raise ValueError("These settings must be positive: " + ", ".join(sorted(invalid)))

    if DB_CONNECT_RETRY_DELAY < 0:
        raise ValueError("DB_CONNECT_RETRY_DELAY must be zero or positive.")
