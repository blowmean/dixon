"""Owner identity for private strategies.

Cloudflare Access authenticates the browser and forwards `Cf-Access-Jwt-Assertion`.
Dixon does not re-verify the signature (the origin only accepts that header from
the tunnel, matching the existing API gate). The `email` claim is the owner.

Localhost and `X-API-Key` callers have no Access email; they share
`DIXON_DEFAULT_OWNER`.
"""

from __future__ import annotations

import base64
import json

from dixon.config import DEFAULT_OWNER


def jwt_email(token: str | None) -> str | None:
    """Return the lowercase `email` claim, or None if the token has none."""
    if not token or token.count(".") < 2:
        return None
    payload_b64 = token.split(".", 2)[1]
    pad = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    email = data.get("email") if isinstance(data, dict) else None
    if not isinstance(email, str) or "@" not in email:
        return None
    return email.strip().lower()


def owner_email(cf_jwt: str | None) -> str:
    """Owner for this request. Raises ValueError if a JWT is present but has no email."""
    if cf_jwt:
        email = jwt_email(cf_jwt)
        if not email:
            raise ValueError("Access JWT is missing an email claim")
        return email
    return DEFAULT_OWNER
