"""Shared application configuration and deterministic environment loading."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

# Hosting-platform environment variables take precedence over local .env values.
load_dotenv(dotenv_path=ENV_FILE, override=False)


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a conventional environment-variable boolean."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
