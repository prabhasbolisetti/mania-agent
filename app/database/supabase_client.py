
"""Centralized Supabase client configuration."""

import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from supabase import Client, create_client


# --------------------------------------------------
# Project paths
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


# --------------------------------------------------
# Environment loading
# --------------------------------------------------

# Environment variables configured by the hosting platform
# take precedence over values from the local .env file.
load_dotenv(
    dotenv_path=ENV_FILE,
    override=False,
)


def _get_env(name: str) -> Optional[str]:
    """Return a non-empty, trimmed environment variable."""
    value = os.getenv(name)

    if value is None:
        return None

    value = value.strip()
    return value or None


# --------------------------------------------------
# Supabase configuration
# --------------------------------------------------

SUPABASE_URL: Optional[str] = _get_env("SUPABASE_URL")

# Support common key variable names without printing their values.
SUPABASE_SECRET_KEY: Optional[str] = (
    _get_env("SUPABASE_SECRET_KEY")
    or _get_env("SUPABASE_SERVICE_ROLE_KEY")
    or _get_env("SUPABASE_KEY")
)


if not SUPABASE_URL:
    raise RuntimeError(
        "SUPABASE_URL is missing. "
        f"Configure it in the environment or in {ENV_FILE}."
    )

SUPABASE_URL = SUPABASE_URL.rstrip("/")

parsed_url = urlparse(SUPABASE_URL)

if (
    parsed_url.scheme != "https"
    or not parsed_url.hostname
    or parsed_url.username
    or parsed_url.password
):
    raise RuntimeError(
        "SUPABASE_URL is invalid. "
        "Use the HTTPS Project URL copied from your Supabase dashboard."
    )


if not SUPABASE_SECRET_KEY:
    raise RuntimeError(
        "Supabase key is missing. Configure one of "
        "SUPABASE_SECRET_KEY, SUPABASE_SERVICE_ROLE_KEY, or SUPABASE_KEY "
        f"in the environment or in {ENV_FILE}."
    )


# --------------------------------------------------
# Create client
# --------------------------------------------------

# This initializes the client but does not prove that the
# remote Supabase host is reachable.
supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SECRET_KEY,
)