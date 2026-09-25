"""The studio's name and public URL. Both are placeholders until the domain is final, so they live here and in
`.env`, never inline. Anything that renders a link a user will paste elsewhere (deployment endpoints, invites)
builds it from public_url(). Read lazily: `.env` is loaded at app startup, after this module is imported."""
from __future__ import annotations

import os


def name() -> str:
    return os.environ.get("STUDIO_NAME", "Impromptune")


def public_url() -> str:
    return os.environ.get("STUDIO_PUBLIC_URL", "https://impromptune.com").rstrip("/")


def app_url() -> str:
    """Where the canvas lives. `public_url()` is the marketing site now, so anything sending a signed-in user
    *back to their work* must use this and not the bare host."""
    return public_url() + os.environ.get("STUDIO_APP_PATH", "/app/")
