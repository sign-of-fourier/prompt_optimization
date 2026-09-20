"""The studio's name and public URL. Both are placeholders until the domain is final, so they live here and in
`.env`, never inline. Anything that renders a link a user will paste elsewhere (deployment endpoints, invites)
builds it from public_url(). Read lazily: `.env` is loaded at app startup, after this module is imported."""
from __future__ import annotations

import os


def name() -> str:
    return os.environ.get("STUDIO_NAME", "Impromptune")


def public_url() -> str:
    return os.environ.get("STUDIO_PUBLIC_URL", "https://impromptune.com").rstrip("/")
