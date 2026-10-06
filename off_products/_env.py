"""Read KEY=VALUE lines from ./.env into os.environ (existing variables win).

Saves Windows users from shell-specific `export` / `$env:` steps.
"""

import os


def load_dotenv(path=".env"):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.split(" #", 1)[0].strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass
