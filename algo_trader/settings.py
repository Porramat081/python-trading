"""Django settings for the algo_trader project.

Configuration is read from environment variables, loaded from a `.env` file
in the project root via python-dotenv. See `.env.example`.
"""
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


# --- Core ---------------------------------------------------------------
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "trading",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "algo_trader.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "trading.context_processors.trading_flags",
            ],
        },
    },
]

WSGI_APPLICATION = "algo_trader.wsgi.application"


# --- Database -----------------------------------------------------------
# SQLite for development; set DATABASE_URL=postgres://... for production
# (requires `pip install "psycopg[binary]"`).
def _database_from_url(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme}")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "CONN_MAX_AGE": 60,
    }


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASES = {
    "default": _database_from_url(DATABASE_URL)
    if DATABASE_URL
    else {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# Times are shown in the stock market's own time zone (US Eastern, with daylight saving).
# Override with DJANGO_TIME_ZONE if needed. The database still stores UTC.
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "America/New_York")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "trading:dashboard"
LOGOUT_REDIRECT_URL = "login"

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


# --- External APIs ------------------------------------------------------
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
# Alpaca market data (free plan: real-time IEX feed). Used for prices only; orders go via SnapTrade.
ALPACA_API_KEY_ID = os.getenv("ALPACA_API_KEY_ID", "")
ALPACA_API_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY", "")
ALPACA_DATA_FEED = os.getenv("ALPACA_DATA_FEED", "iex")  # "sip" needs a paid Alpaca data plan
MASSIVE_BASE_URL = os.getenv("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")

SNAPTRADE_CLIENT_ID = os.getenv("SNAPTRADE_CLIENT_ID", "")
SNAPTRADE_CONSUMER_KEY = os.getenv("SNAPTRADE_CONSUMER_KEY", "")
SNAPTRADE_REDIRECT_URL = os.getenv("SNAPTRADE_REDIRECT_URL", "")
# "commercial" (standard partner keys) or "personal" (SnapTrade personal API keys)
SNAPTRADE_KEY_TYPE = os.getenv("SNAPTRADE_KEY_TYPE", "commercial").strip().lower()

# --- US market hours (header clock + scheduler), in TIME_ZONE local time --------
MARKET_OPEN_TIME = "09:30"
MARKET_CLOSE_TIME = "16:00"

# --- Trading safety switches -------------------------------------------
# TRADING_ENABLED=False -> signals are computed and logged but no orders are sent.
TRADING_ENABLED = env_bool("TRADING_ENABLED", False)
# LIVE_TRADING=False -> onboarding links connect the Alpaca *paper* brokerage.
LIVE_TRADING = env_bool("LIVE_TRADING", False)
SNAPTRADE_BROKER_SLUG = "ALPACA" if LIVE_TRADING else "ALPACA-PAPER"


# --- Logging ------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S %Z",  # e.g. 2026-11-03 10:15:00 EST
        }
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "services": {"handlers": ["console"], "level": "INFO"},
        "trading": {"handlers": ["console"], "level": "INFO"},
    },
}
