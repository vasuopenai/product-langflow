"""Settings from the environment (and ./.env; existing variables win)."""

import os


def load_dotenv(path=".env"):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.split(" #", 1)[0].strip().strip("'\""))
    except FileNotFoundError:
        pass


load_dotenv()


def env(name, default=None):
    return os.getenv(name, default)


def dsn():
    return os.environ["DATABASE_URL"]


USDA_API_KEY = env("USDA_API_KEY", "DEMO_KEY")  # free key: https://api.data.gov/signup/
WEB_SEARCH_MODEL = env("WEB_SEARCH_MODEL", "gpt-4o")
AT_STORE_METERS = float(env("AT_STORE_METERS", "200"))
APP_KEY = env("MOBILE_APP_KEY")      # if set, the app must send X-App-Key
ADMIN_KEY = env("MOBILE_ADMIN_KEY")  # if set, review endpoints need X-Admin-Key
# Paid calls per client per hour (questions, web searches, barcode research).
HOURLY_LIMIT = int(env("MOBILE_HOURLY_LIMIT", "60"))
# Shown on the /privacy and /support pages (App Store requires both).
SUPPORT_EMAIL = env("SUPPORT_EMAIL")
PRIVACY_UPDATED = env("PRIVACY_UPDATED", "October 9, 2026")
